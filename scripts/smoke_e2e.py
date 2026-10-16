"""M1 手工端到端验证脚本（施工单 §4 验收门禁的自动化版本）。

跑法（服务需已在 8000 端口启动）：
    python scripts/smoke_e2e.py

它做的事：建店 → 灌 sample.json 的 7 天 → 取报告 → 打印人看的结论。
这是"开发者本人在手机/命令行上真实走一遍"的替身，输出留档作为验收证据。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "tests" / "fixtures" / "sample.json"


def call(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=20) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:  # pragma: no cover - 出问题时要看见原文
        print(f"!! {method} {path} -> HTTP {exc.code}\n{exc.read().decode('utf-8')}")
        raise


def main() -> int:
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))

    shop = call("POST", "/api/shops", {"name": sample["shop"]["name"], "default_hourly_rate": 25})
    shop_id = shop["id"]
    print(f"[1] 建店 -> id={shop_id}")

    call(
        "PUT",
        f"/api/shops/{shop_id}/monthly",
        {"month": sample["monthly"]["month"], "values": sample["monthly"]["values"]},
    )
    print(f"[2] 月固定开销 -> {sample['monthly']['values']}")

    for day in sample["daily"]:
        got = call(
            "PUT",
            "/api/daily",
            {
                "shop_id": shop_id,
                "stat_date": day["stat_date"],
                "values": day["values"],
                "shifts": day["shifts"],
            },
        )
        hours = sum(s["hours"] for s in got["shifts"])
        print(f"[3] {day['stat_date']} 录入 ok（工时 {hours} 小时）")

    report = call(
        "GET",
        f"/api/report?shop_id={shop_id}&start=2026-10-01&end=2026-10-07",
    )

    print("\n================ 报告（老板看到的样子）================")
    for line in report["conclusion"]["lines"]:
        print("  " + line)
    for warn in report["conclusion"]["warnings"]:
        print("  ⚠ " + warn)

    print("\n---------------- 指标卡 ----------------")
    for m in report["metrics"]:
        bench = m.get("benchmark") or {}
        flag = {"ok": "", "high": " ↑偏高", "low": " ↓偏低", "none": ""}.get(bench.get("status", ""), "")
        print(f"  {m['name']:<14} {m['display']:>12} {flag}")

    print("\n---------------- 观察项 ----------------")
    obs = report["observations"]
    print(f"  合计营业额 {obs['total_revenue']}，日均 {obs['daily_avg_revenue']}")
    print(f"  工作日日均 {obs['weekday_avg_revenue']} / 周末日均 {obs['weekend_avg_revenue']}")
    print(f"  最好的一天 {obs['best_day']}")

    expected = sample["_手工核对结果_Excel口径"]
    print("\n---------------- 与手工 Excel 对账 ----------------")
    values = {m["code"]: m["value"] for m in report["metrics"]}
    checks = {
        "revenue": expected["revenue"],
        "purchase": expected["purchase"],
        "orders": expected["orders"],
        "hours": expected["hours"],
        "labor_cost": expected["labor_cost"],
        "avg_ticket": 23000 / 755,
        "efficiency": 23000 / 38,
        "breakeven_daily": 469.4444444444445,
        # M2-1 新增：本样例的月行只填了房租/水电/毛利率，
        # 所以 labor_m_full 必须**退化为 labor_m**（不是 None、不是 0）
        "labor_m_full": expected["labor_cost"],
        "labor_m": expected["labor_cost"],
    }
    ok = True
    for code, want in checks.items():
        got = values.get(code)
        good = got is not None and abs(got - want) < 0.01
        ok = ok and good
        print(f"  {'OK ' if good else 'FAIL'} {code:<18} 引擎 {got} / 手工 {round(want, 6)}")

    labor = report["observations"].get("labor") or {}
    print(
        f"\n  月口径人工对照（{labor.get('month_anchor')}）："
        f"排班工钱 {labor.get('labor_m_direct')} / 全口径 {labor.get('labor_m_full')} "
        f"/ 差额 {labor.get('gap')}"
    )
    print(f"  结论第三行：{report['conclusion']['lines'][2]}")
    print(f"  环比段条数：{len(report.get('trend') or [])}")

    print("\n结果：", "全部一致 ✅" if ok else "存在偏差 ❌")

    # M3-7 演示说明（docs/03 §6）：这个样例的食材率是 25%，
    # 在 M2-0 把快餐区间改成 28%–38% 之后，它落在**下限之外**，所以判「偏低」。
    # 这是样例数据本身的口径问题（它是对账基线，不是基准标定数据），不是引擎出错。
    food = next((m for m in report["metrics"] if m["code"] == "food_cost_rate"), None)
    if food is not None and (food.get("benchmark") or {}).get("status") == "low":
        print(
            "\nℹ️  说明：本样例的食材率 25% 低于快餐基准 28%–38%，所以上面判「偏低」。\n"
            "    要演示「偏高」的话术，请用 3 年合成数据（Y3 = 41%）或真实店数据 ——\n"
            "    样例 fixtures/sample.json 是**对账基线**，数值刻意保持不变。"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
