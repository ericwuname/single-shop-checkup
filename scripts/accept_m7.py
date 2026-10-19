#!/usr/bin/env python3
"""M7 验收脚本（验收方：评审窗口。执行方可以先自己跑，红了就别交付）。

只用标准库，Windows / Linux 都能跑。**不改代码，只发 HTTP 请求。**

用法（服务已启动、web/dist 已构建并由后端挂载在 / ）：

    # 本机开发模式（M7-2 / M7-3 的检查）
    python scripts/accept_m7.py --base http://127.0.0.1:8000 --allow-write

    # 公网模式（M7-1 的检查；服务要带 PUBLIC_MODE=1 和 APP_ACCESS_KEY 启动）
    python scripts/accept_m7.py --base http://127.0.0.1:8000 --public --key <访问码> --allow-write

--allow-write：允许脚本新建一家名为「验收临时店」的店来取报告。**只能对测试库用**，
               绝不能对真实数据库用（删除需要 ADMIN_TOKEN，脚本不会碰）。

退出码：0 = 全部通过；1 = 有失败项。每项都打印 PASS / FAIL / SKIP 和原因。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import date

RESULTS: list[tuple[str, str, str]] = []

JARGON = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")
EVIL_ORIGIN = "https://evil.example"


def record(status: str, name: str, detail: str = "") -> None:
    RESULTS.append((status, name, detail))
    mark = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭ "}[status]
    print(f"{mark} {status}  {name}" + (f"  —— {detail}" if detail else ""))


def check(cond: bool, name: str, detail_fail: str = "") -> None:
    record("PASS" if cond else "FAIL", name, "" if cond else detail_fail)


def http(base: str, method: str, path: str, *, headers: dict | None = None, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs = {"Content-Type": "application/json"} if body is not None else {}
    hdrs.update(headers or {})
    req = urllib.request.Request(base + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
    except Exception as exc:  # noqa: BLE001
        return 0, {}, str(exc).encode()


def jget(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def lower_headers(h: dict) -> dict:
    return {k.lower(): v for k, v in h.items()}


# --------------------------------------------------------------------------- #
# M7-1 访问控制
# --------------------------------------------------------------------------- #
def check_access_control(base: str, key: str) -> None:
    print("\n== M7-1 访问控制（公网模式）==")
    st, _, body = http(base, "GET", "/api/health")
    j = jget(body) or {}
    check(st == 200, "1.1 /api/health 无需访问码即可探活", f"状态 {st}")
    check("llm" not in j, "1.2 公网模式下 /api/health 不回显 llm 配置", "还在返回 llm 字段")

    st, _, _ = http(base, "GET", "/api/shops")
    check(st == 401, "1.3 不带访问码访问 /api/shops → 401", f"实际 {st}（没拦住 = 任何人能读全部门店）")

    st, _, _ = http(base, "GET", "/api/shops", headers={"X-Access-Key": "wrong-" + key})
    check(st == 401, "1.4 访问码错误 → 401", f"实际 {st}")

    st, _, _ = http(base, "GET", "/api/shops", headers={"X-Access-Key": key})
    check(st == 200, "1.5 访问码正确 → 200", f"实际 {st}")

    st, _, _ = http(base, "POST", "/api/shops", body={"name": "不该建成"})
    check(st == 401, "1.6 不带访问码 POST 建店 → 401", f"实际 {st}")

    for path in ("/docs", "/redoc", "/openapi.json"):
        st, _, _ = http(base, "GET", path)
        check(st in (401, 403, 404), f"1.7 公网模式下 {path} 不公开", f"实际 {st}")

    st, hdrs, _ = http(
        base, "GET", "/api/health", headers={"Origin": EVIL_ORIGIN}
    )
    acao = lower_headers(hdrs).get("access-control-allow-origin", "")
    check(acao not in ("*", EVIL_ORIGIN), "1.8 陌生来源不被 CORS 放行", f"Access-Control-Allow-Origin={acao!r}")

    st, _, body = http(base, "GET", "/")
    check(st == 200 and b"<html" in body.lower(), "1.9 前端页面（/）无需访问码即可打开（访问码在页面里输入）", f"状态 {st}")


# --------------------------------------------------------------------------- #
# M7-2 界面缺陷（D1 / D2）——看报告接口返回的“老板能看到的字”
# --------------------------------------------------------------------------- #
def visible_strings(report: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def walk(prefix: str, node) -> None:
        if isinstance(node, str):
            out.append((prefix, node))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(f"{prefix}[{i}]", v)
        elif isinstance(node, dict):
            for k, v in node.items():
                if k in {"code", "metric_code", "key", "benchmark_key", "unit", "layer", "status", "tier"}:
                    continue  # 机器字段，不是给老板看的
                walk(f"{prefix}.{k}", v)

    for part in ("conclusion", "observations", "anomalies", "blocked"):
        if part in report:
            walk(part, report[part])
    for i, m in enumerate(report.get("metrics") or []):
        for k in ("name", "display", "sentence"):
            if isinstance(m.get(k), str):
                out.append((f"metrics[{i}].{k}", m[k]))
        bm = m.get("benchmark")
        if isinstance(bm, dict):
            walk(f"metrics[{i}].benchmark", bm)
    return out


def check_report_text(base: str, headers: dict, allow_write: bool) -> None:
    print("\n== M7-2 老板能看到的字（D1 术语泄露 / D2 空数据自相矛盾）==")
    if not allow_write:
        record("SKIP", "2.x 需要 --allow-write（要新建一家临时店取报告）")
        return
    st, _, body = http(base, "POST", "/api/shops", headers=headers, body={"name": "验收临时店"})
    shop = jget(body) or {}
    if st != 201 or "id" not in shop:
        record("FAIL", "2.0 建临时店失败", f"状态 {st} {body[:120]!r}")
        return
    sid = shop["id"]
    today = date.today().isoformat()

    st, _, body = http(base, "GET", f"/api/report?shop_id={sid}&start={today}&end={today}", headers=headers)
    rep = jget(body) or {}
    check(st == 200 and "conclusion" in rep, "2.1 空店能取到报告", f"状态 {st}")
    texts = visible_strings(rep)

    leaks = [(p, t) for p, t in texts if JARGON.search(t)]
    check(
        not leaks,
        "2.2 D1：老板可见文字里没有 snake_case 内部字段名",
        "；".join(f"{p}: {JARGON.search(t).group(0)}" for p, t in leaks[:6]),
    )

    lines = (rep.get("conclusion") or {}).get("lines") or []
    empty_state = bool(lines) and "还没填" in lines[0]
    if empty_state:
        contradict = len(lines) > 1 and "正常区间" in lines[1]
        check(
            not contradict,
            "2.3 D2：一个数都没填时，不说“看着都在正常区间”",
            f"第二行仍是：{lines[1]!r}",
        )
    else:
        record("SKIP", "2.3 D2：空店没有进入“还没填”分支，跳过", f"lines[0]={lines[:1]}")

    # D3：基础档报告页——有值的指标要排在前面，不能 20 多个 "--" 先出场
    metrics = rep.get("metrics") or []
    leading_blank = 0
    for m in metrics:
        if m.get("display") in (None, "--", ""):
            leading_blank += 1
        else:
            break
    record(
        "PASS",
        "2.4 D3 仅提示：接口层不强制排序（排序/折叠由前端完成，见浏览器验收）",
        f"空店共 {len(metrics)} 个指标，其中前导空值 {leading_blank} 个",
    )


# --------------------------------------------------------------------------- #
# M7-3 PWA
# --------------------------------------------------------------------------- #
def check_pwa(base: str) -> None:
    print("\n== M7-3 PWA / 可安装 ==")
    st, hdrs, body = http(base, "GET", "/manifest.json")
    mf = jget(body) or {}
    check(st == 200 and bool(mf), "3.1 manifest.json 可取", f"状态 {st}")
    if not mf:
        return
    check(mf.get("display") == "standalone", "3.2 display = standalone", f"实际 {mf.get('display')!r}")
    check(str(mf.get("theme_color", "")).upper() == "#0E6B58", "3.3 theme_color = #0E6B58（新主色）", f"实际 {mf.get('theme_color')!r}")
    check(str(mf.get("lang", "")).lower().startswith("zh"), "3.4 lang = zh-CN", f"实际 {mf.get('lang')!r}")
    check(mf.get("start_url") in ("/", "./", "/?source=pwa"), "3.5 start_url 合理", f"实际 {mf.get('start_url')!r}")

    icons = mf.get("icons") or []
    sizes = {i.get("sizes") for i in icons}
    check({"192x192", "512x512"} <= sizes, "3.6 icons 含 192x192 与 512x512", f"实际 sizes={sorted(s for s in sizes if s)}")
    check(any("maskable" in str(i.get("purpose", "")) for i in icons), "3.7 含 maskable 图标（安卓自适应图标）", "没有 purpose=maskable 的图标")

    bad: list[str] = []
    for icon in icons:
        src = icon.get("src", "")
        path = src if src.startswith("/") else "/" + src.lstrip("./")
        st, hdrs, data = http(base, "GET", path)
        ctype = lower_headers(hdrs).get("content-type", "")
        if st != 200 or "image/png" not in ctype or not data.startswith(b"\x89PNG"):
            bad.append(f"{src}({st},{ctype})")
    check(not bad, "3.8 每个图标文件都能取到且是真 PNG", "；".join(bad))

    st, hdrs, body = http(base, "GET", "/sw.js")
    ctype = lower_headers(hdrs).get("content-type", "")
    check(st == 200 and "javascript" in ctype, "3.9 /sw.js 可取且是 JS", f"状态 {st} {ctype}")
    text = body.decode("utf-8", "ignore")
    check("/api/" in text, "3.10 sw.js 对 /api/ 有明确处理（必须网络直连，不缓存接口）", "sw.js 里没出现 /api/")
    check(not re.search(r"cache\.(put|add)\([^)]*api", text, re.I), "3.11 sw.js 没有缓存 /api/ 的写法", "疑似缓存了接口响应")

    st, _, body = http(base, "GET", "/")
    html = body.decode("utf-8", "ignore")
    check('rel="manifest"' in html, '3.12 index.html 有 <link rel="manifest">', "缺少")
    check('name="theme-color"' in html, "3.13 index.html 有 theme-color", "缺少")
    check('lang="zh' in html, "3.14 index.html lang=zh-CN", "缺少")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--key", default="", help="APP_ACCESS_KEY（公网模式需要）")
    ap.add_argument("--public", action="store_true", help="按公网模式检查访问控制")
    ap.add_argument("--allow-write", action="store_true", help="允许新建临时店（仅测试库）")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print(f"验收对象：{base}   公网模式检查：{'是' if args.public else '否'}")
    st, _, _ = http(base, "GET", "/api/health")
    if st != 200:
        print(f"❌ 服务不可达（/api/health → {st}）。先启动服务。")
        return 1

    headers = {"X-Access-Key": args.key} if args.key else {}
    if args.public:
        if not args.key:
            print("❌ --public 需要同时给 --key")
            return 1
        check_access_control(base, args.key)
    check_report_text(base, headers, args.allow_write)
    check_pwa(base)

    fails = [r for r in RESULTS if r[0] == "FAIL"]
    passes = [r for r in RESULTS if r[0] == "PASS"]
    skips = [r for r in RESULTS if r[0] == "SKIP"]
    print(f"\n合计：{len(passes)} 通过 / {len(fails)} 失败 / {len(skips)} 跳过")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
