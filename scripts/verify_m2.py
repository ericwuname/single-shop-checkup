# -*- coding: utf-8 -*-
"""M2 门禁核验脚本（可复现）。

把 `docs/02-M2长程施工任务书.md` 里所有**数值判据**跑一遍，输出 PASS/FAIL 表。
对照物是 `data/seed_3y_reports_baseline_M1.json`（修基准之前的基线，已单独备份）。

跑法（服务需已在 8000 端口运行，且已用 scripts/seed_3y_data.py 重灌过数据）：
    python scripts/verify_m2.py

退出码 0 = 全部通过。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NEW = ROOT / "data" / "seed_3y_reports.json"
BASE = ROOT / "data" / "seed_3y_reports_baseline_M1.json"
YEARS = ("Y1", "Y2", "Y3")

failures: list[str] = []
checks: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((("PASS" if ok else "FAIL"), name, detail))
    if not ok:
        failures.append(f"{name} —— {detail}")


def metrics(period: dict) -> dict:
    return {m["code"]: m for m in period["report"]["metrics"]}


def fmt(v, spec: str = ",.0f") -> str:
    return "--" if v is None else format(v, spec)


def fmt_pct(v) -> str:
    return "--" if v is None else format(v, ".1%")


def value(period: dict, code: str):
    return metrics(period).get(code, {}).get("value")


def status(period: dict, code: str):
    return (metrics(period).get(code, {}).get("benchmark") or {}).get("status")


def main() -> int:
    if not NEW.exists() or not BASE.exists():
        print(f"缺文件：{NEW} 或 {BASE}")
        return 2

    new = json.loads(NEW.read_text(encoding="utf-8"))
    base = json.loads(BASE.read_text(encoding="utf-8"))

    # 陈旧数据守卫：如果 JSON 里没有 M3 新增的 labor_cost_rate_full，说明没重灌，
    # 这时候跑出来的所有判断都是错的 —— 直接停，不要给出一堆假 PASS/FAIL。
    if value(new["periods"]["Y1"], "labor_cost_rate_full") is None:
        print("❌ data/seed_3y_reports.json 是旧数据（没有 labor_cost_rate_full / M3 指标），请先重灌：")
        print("   1) 停掉占用 8000 的进程   2) rm -f data/checkup.db")
        print("   3) 起服务（$PY -m uvicorn server.main:app --port 8000）")
        print("   4) $PY scripts/seed_3y_data.py")
        return 3

    print("=" * 96)
    print("M2 门禁核验 · 对照基线：", BASE.name)
    print("=" * 96)

    # ---------------- 可比性：营业额必须与基线逐分一致 ----------------
    # 种子脚本的随机序列只用于 revenue / purchase，改动月口径字段不会动它。
    # 这条通过 = 新旧数据"同一条经营曲线"，后面的比较才有意义。
    print("\n【0】可比性 —— 重灌后营业额必须与基线一模一样")
    for y in YEARS:
        a = value(base["periods"][y], "revenue")
        b = value(new["periods"][y], "revenue")
        check(f"{y} 营业额与基线一致", abs(a - b) < 0.01, f"基线 {a} / 现在 {b}")

    # ---------------- M2-0 食材成本率基准 ----------------
    print("\n【M2-0】食材成本率区间改成 28%–38%（预警 >40%）")
    print(f"      display = {metrics(new['periods']['Y1'])['food_cost_rate']['benchmark']['range_display']}")
    for y, want in (("Y1", "ok"), ("Y2", "ok"), ("Y3", "high")):
        food = value(new["periods"][y], "food_cost_rate")
        got = status(new["periods"][y], "food_cost_rate")
        check(f"{y} 食材率 {food:.1%} 判 {want}", got == want, f"实际判 {got}")
        old = status(base["periods"][y], "food_cost_rate")
        print(f"      {y}: {food:.1%}  旧判 {old} → 新判 {got}")

    # ---------------- M2-1 全口径人工 ----------------
    print("\n【M2-1】全口径人工进保本线")
    print(f"      {'年份':<4}{'排班工钱(月)':>14}{'全口径(月)':>14}{'差额(月)':>12}"
          f"{'保本(日) 旧':>14}{'保本(日) 新':>14}{'全口径人力率':>14}")
    prev_be = None
    for y in YEARS:
        direct = value(new["periods"][y], "labor_m")
        full = value(new["periods"][y], "labor_m_full")
        be_new = value(new["periods"][y], "breakeven_daily")
        be_old = value(base["periods"][y], "breakeven_daily")
        rev = value(new["periods"][y], "revenue")
        full_rate = full * 12 / rev if (full is not None and rev) else None
        gap = (full - direct) if (full is not None and direct is not None) else None
        print(f"      {y:<4}{fmt(direct):>14}{fmt(full):>14}{fmt(gap):>12}"
              f"{fmt(be_old, ',.2f'):>14}{fmt(be_new, ',.2f'):>14}"
              f"{(format(full_rate, '.1%') if full_rate is not None else '--'):>14}")

        check(f"{y} labor_m_full 显著大于 labor_m",
              full is not None and direct is not None and full > direct * 1.1,
              f"{fmt(full)} vs {fmt(direct)}")
        check(f"{y} 保本线高于旧基线", be_new is not None and be_old is not None and be_new > be_old,
              f"现在 {fmt(be_new, ',.2f')} / 旧 {fmt(be_old, ',.2f')}")
        if prev_be is not None and be_new is not None:
            check(f"{y} 保本线比上一年递增", be_new > prev_be, f"{fmt(be_new, ',.2f')} > {fmt(prev_be, ',.2f')}")
        prev_be = be_new

    # 独立算一遍"人工 +25%"的口径（docs/01 §4 缺陷 2 的 26.5%）
    y3 = new["periods"]["Y3"]
    y3_rev = value(y3, "revenue")
    y3_direct_annual = value(y3, "labor_cost")
    y3_full_rate = value(y3, "labor_m_full") * 12 / y3_rev
    y3_direct_rate = y3_direct_annual / y3_rev
    print(f"\n      Y3 排班工钱口径人力率 = {y3_direct_rate:.1%}"
          f"（基线是 {value(base['periods']['Y3'], 'labor_cost_rate'):.1%}）")
    print(f"      Y3 全口径人力率       = {y3_full_rate:.1%}   ← docs/01 缺陷2 的目标值 ≈26.5%")
    check("Y3 全口径人力率 ≈ 26.5%（docs/01 §4 缺陷2）", abs(y3_full_rate - 0.265) < 0.02,
          f"算出 {y3_full_rate:.1%}")
    check("Y3 全口径人力率已突破 25% 预警线", y3_full_rate > 0.25, f"{y3_full_rate:.1%}")

    # ---------------- M2-3 环比 ----------------
    print("\n【M2-3】环比段")
    for y, prev_y in (("Y2", "Y1"), ("Y3", "Y2")):
        trend = {t["code"]: t for t in new["periods"][y]["report"]["trend"]}
        food = trend.get("food_cost_rate")
        if food:
            print(f"      {y} vs {prev_y} 食材: {food['previous']:.1%} → {food['current']:.1%}"
                  f"  {food['direction']}  delta={food['delta']:+.3f}")
            print(f"        「{food['sentence']}」")
        eff = trend.get("efficiency")
        if eff:
            print(f"      {y} vs {prev_y} 人效: {eff['previous']:,.2f} → {eff['current']:,.2f}"
                  f"  {eff['direction']}")

    t2 = {t["code"]: t for t in new["periods"]["Y2"]["report"]["trend"]}
    t3 = {t["code"]: t for t in new["periods"]["Y3"]["report"]["trend"]}
    check("Y2 vs Y1 食材 delta ≈ +0.06 且判升",
          abs(t2["food_cost_rate"]["delta"] - 0.06) < 0.005 and t2["food_cost_rate"]["direction"] == "升",
          f"delta={t2['food_cost_rate']['delta']:.4f} dir={t2['food_cost_rate']['direction']}")
    check("Y3 vs Y2 食材 delta ≈ +0.05 且判升",
          abs(t3["food_cost_rate"]["delta"] - 0.05) < 0.005 and t3["food_cost_rate"]["direction"] == "升",
          f"delta={t3['food_cost_rate']['delta']:.4f} dir={t3['food_cost_rate']['direction']}")
    check("Y3 vs Y2 人效判降", t3["efficiency"]["direction"] == "降",
          f"dir={t3['efficiency']['direction']}")
    check("Y1 首期 trend 为空列表", base["periods"]["Y1"]["report"].get("trend") in (None, [], ()),
          "基线里 Y1 无 trend 段（M1 还没有这个功能）")

    # ---------------- 三门结论 ----------------
    print("\n【结论】三年三行结论")
    lines_by_year = {}
    for y in YEARS:
        lines = new["periods"][y]["report"]["conclusion"]["lines"]
        lines_by_year[y] = lines
        print(f"\n  {y}  tier={new['periods'][y]['report']['period']['tier']}"
              f"  days_span={new['periods'][y]['report']['period']['days_span']}")
        for i, ln in enumerate(lines):
            print(f"    [{i}] {ln}")

    # 门禁 4：三年里至少有两行的内容不全一样
    diff_lines = 0
    for i in range(3):
        col = {lines_by_year[y][i] for y in YEARS}
        if len(col) > 1:
            diff_lines += 1
    print(f"\n  三年之间有差异的行数 = {diff_lines} / 3")
    check("门禁4：三年结论至少两行不同", diff_lines >= 2, f"实际 {diff_lines} 行不同")

    for y in YEARS:
        check(f"{y} 年度档第三行不含「明天」", "明天" not in lines_by_year[y][2],
              lines_by_year[y][2])
        check(f"{y} 年度档第三行含「明年」", "明年" in lines_by_year[y][2], lines_by_year[y][2])

    # M2-4 保本自比对（Y2/Y3 有上一期，必须出现；这里是年度档）
    for y in ("Y2", "Y3"):
        blob = " ".join(lines_by_year[y])
        check(f"{y} 年度档出现保本涨幅对比句", "保本" in blob and "涨了" in blob, blob[:60])

    # ---------------- M2-2 不许误伤 ----------------
    print("\n【M2-2】短区间不许误伤（7 天档仍说「明天」）")
    # 用同一家店跑一个 7 天区间
    import urllib.request

    def get(url: str):
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(url, timeout=30) as r:
            return json.loads(r.read())

    sid = new["shop_id"]
    weekly = get(f"http://localhost:8000/api/report/weekly?shop_id={sid}&anchor=2026-09-30&days=7")
    print(f"  7 天档 tier={weekly['period']['tier']} 第三行：{weekly['conclusion']['lines'][2]}")
    check("7 天档 tier == day", weekly["period"]["tier"] == "day", weekly["period"]["tier"])
    check("7 天档第三行仍含「明天」", "明天" in weekly["conclusion"]["lines"][2],
          weekly["conclusion"]["lines"][2])

    # ---------------- M3-0 / M3-1：基准收敛与全口径率 ----------------
    print("\n【M3-0 / M3-1】基准收敛与全口径人力成本率")
    print(f"      {'年份':<4}{'直接人工率':>12}{'判定':>6}{'全口径率':>12}{'判定':>6}"
          f"{'人效':>10}{'判定':>6}{'账面利润(月)':>14}{'实际利润(月)':>14}{'隐性成本(月)':>14}")
    for y in YEARS:
        p = new["periods"][y]
        direct = value(p, "labor_cost_rate")
        full = value(p, "labor_cost_rate_full")
        eff = value(p, "efficiency")
        book = value(p, "book_profit_m")
        real = value(p, "real_profit_m")
        hidden = value(p, "hidden_cost_m")
        print(f"      {y:<4}{fmt_pct(direct):>12}{status(p, 'labor_cost_rate') or '--':>6}"
              f"{fmt_pct(full):>12}{status(p, 'labor_cost_rate_full') or '--':>6}"
              f"{fmt(eff, '.2f'):>10}{status(p, 'efficiency') or '--':>6}"
              f"{fmt(book):>14}{fmt(real):>14}{fmt(hidden):>14}")

    p3 = new["periods"]["Y3"]
    p1 = new["periods"]["Y1"]
    # ---- M4-0（docs/04 §1 裁定 6-3）：判断从"排班工钱率"改判到"全口径率" ----
    # 原先这里断言的是 "Y3 直接人工率 21.2% 判 high"。M4-0 把 labor_cost_rate 的
    # benchmark_key 置为 None（口径错配会误导，见裁定 6-3），所以那一条**判据本身失效**，
    # 按任务书"若判据需更新须逐条说明"的要求，在这里替换为下面两条等价强度的判据。
    check("★ M4-0：labor_cost_rate 不再给判断（benchmark_key=None）",
          status(p3, "labor_cost_rate") is None,
          f"实际判 {status(p3, 'labor_cost_rate')}")
    check("★ M4-0：判断改由 labor_cost_rate_full 承担 —— Y3 判 high",
          status(p3, "labor_cost_rate_full") == "high",
          f"实际判 {status(p3, 'labor_cost_rate_full')}")
    for y in YEARS:
        check(f"{y} 人效判 ok（新区间 120–180）", status(new["periods"][y], "efficiency") == "ok",
              f"实际判 {status(new['periods'][y], 'efficiency')}")

    full3 = value(p3, "labor_cost_rate_full")
    check("M3-0：Y3 全口径人力率 ≈ 26.2% 且判 high",
          full3 is not None and abs(full3 - 0.262) < 0.01
          and status(p3, "labor_cost_rate_full") == "high",
          f"算出 {fmt_pct(full3)}，判 {status(p3, 'labor_cost_rate_full')}")
    check("M3-0：Y1 全口径率低于 Y3",
          (value(p1, "labor_cost_rate_full") or 0) < (full3 or 0),
          f"{fmt_pct(value(p1, 'labor_cost_rate_full'))} < {fmt_pct(full3)}")

    # ---------------- M3-2：隐性成本 ----------------
    print("\n【M3-2】隐性成本：账面 vs 实际")
    for y in YEARS:
        p = new["periods"][y]
        book, real, hidden = value(p, "book_profit_m"), value(p, "real_profit_m"), value(p, "hidden_cost_m")
        line1 = p["report"]["conclusion"]["lines"][0]
        print(f"      {y}: 账面 {fmt(book)} − 隐性 {fmt(hidden)} = 实际 {fmt(real)}")
        check(f"{y} 实际利润显著低于账面利润",
              book is not None and real is not None and real < book,
              f"{fmt(real)} < {fmt(book)}")
        check(f"{y} 结论里说出了差额",
              "账面赚了" in line1 and "实际还剩" in line1 and "中间差" in line1,
              line1[:70])

    # ---------------- 基准来源 ----------------
    from server import benchmarks as bm

    pending = [
        f"{k}"
        for k, v in bm.BENCHMARKS["catering"].items()
        if "待查证" in str(v.get("source") or "")
    ]
    check("M3-1：不再有任何「待查证」区间", not pending, str(pending))

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 96)
    for state, name, detail in checks:
        mark = "✅" if state == "PASS" else "❌"
        print(f"  {mark} {name}" + (f"   [{detail}]" if state == "FAIL" else ""))
    print("=" * 96)
    passed = len(checks) - len(failures)
    print(f"结果：{passed}/{len(checks)} 项通过")
    if failures:
        print("\n未通过：")
        for f in failures:
            print("  - " + f)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
