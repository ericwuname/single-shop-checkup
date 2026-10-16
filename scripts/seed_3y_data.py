# -*- coding: utf-8 -*-
"""3 年餐饮单店测试数据 —— 通过 HTTP API 真实录入（端到端，不直接写库）。

参数来源（2026-10-16 联网查证的公开行业数据）：
- 社区快餐/面馆：客单价 16–30 元，食材成本率 30%–41%，房租占营收 8%–15%，
  水电燃气 2.5%–4%，周末及节假日营业额上浮 30%–55%，
  3–4 月与 11 月为淡季（约为旺季的 60%–80%），
  人工因社保合规年涨 20%–27%（查证值）。
- 本脚本用「营业额涨、成本涨更快」的曲线，用于检验系统能否抓出
  「有流水没利润」这一典型病症。

可复现：random.seed 固定。
"""

import datetime as dt
import json
import random
import sys
import time
import urllib.request
from pathlib import Path

# 用 127.0.0.1 而不是 localhost：Windows 上 Python 解析 localhost 会先试 IPv6(::1)，
# 失败后回退到 IPv4，**每次请求白等约 2 秒**（实测 2.07s vs 0.22s）。
# 1111 天就是 40 分钟 vs 4 分钟的区别。
BASE = "http://127.0.0.1:8000"
random.seed(20261016)

# 本机浏览器/工具链常会设置 http_proxy；urllib 默认会把它套到本机地址上，
# 结果是 502 Bad Gateway（不是服务的问题）。这里显式绕过代理，让脚本换台机器也能跑。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(method: str, path: str, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with _OPENER.open(r, timeout=60) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


# ---------------- 波动模型 ----------------
MONTH_F = {1: 1.35, 2: 1.18, 3: 0.80, 4: 0.82, 5: 0.95, 6: 0.95,
           7: 1.18, 8: 1.22, 9: 1.00, 10: 1.05, 11: 0.78, 12: 1.12}
WEEK_F = {0: 0.85, 1: 0.95, 2: 0.95, 3: 1.00, 4: 1.15, 5: 1.40, 6: 1.35}

HOLIDAYS = set()
for _y, _sd in ((2024, dt.date(2024, 2, 10)), (2025, dt.date(2025, 1, 29)), (2026, dt.date(2026, 2, 17))):
    for _i in range(8):
        HOLIDAYS.add(_sd + dt.timedelta(days=_i))
for _y in (2024, 2025, 2026):
    for _i in range(3):
        HOLIDAYS.add(dt.date(_y, 1, 1) + dt.timedelta(days=_i))
    for _i in range(5):
        HOLIDAYS.add(dt.date(_y, 5, 1) + dt.timedelta(days=_i))
    for _i in range(7):
        HOLIDAYS.add(dt.date(_y, 10, 1) + dt.timedelta(days=_i))

# 三个经营年度的"病症曲线"：营业额涨 25%，成本涨得更快
#
# M3-2 追加 `hidden`（隐性成本，月口径）。取值来自 docs/03 §3 M3-2 的查证区间：
#   员工餐 = 人数 × 15 元/天 × 30 天；餐具年 0.6–1.2 万；维修年 0.5–2 万；
#   收银差异 = 流水 0.5%（查证区间 0.3%–1%）；退菜报废小额。
# 加这五个字段**不影响 revenue/purchase 的随机序列**（随机只用在 rev/pur 上），
# 所以三年营业额仍与 M1 基线逐分一致 —— 新旧数据依然可比。
PLAN = {
    1: dict(base=3200, ticket=26, food=0.30, n_full=3, full_monthly=14400,
            part_rate=25, rent=11000, util=2800,
            hidden=dict(staff_meal=1350, tableware=700, repair=900,
                        cash_diff=480, returned=600)),
    2: dict(base=3800, ticket=28, food=0.36, n_full=3, full_monthly=18000,
            part_rate=28, rent=11880, util=3100,
            hidden=dict(staff_meal=1350, tableware=800, repair=1000,
                        cash_diff=570, returned=700)),
    3: dict(base=4000, ticket=29, food=0.41, n_full=4, full_monthly=24800,
            part_rate=30, rent=12830, util=3400,
            hidden=dict(staff_meal=1800, tableware=900, repair=1200,
                        cash_diff=600, returned=800)),
}
FULL_SLOTS = [("06:00", "14:00"), ("14:00", "22:00"), ("10:00", "19:00"), ("11:00", "20:00")]

# ---- M2-1 新增：全口径人工的附加成本比例（单位部分，以"全职月薪"为基数）----
# 依据（常识性行业口径，非本脚本自行查证）：社保单位部分约 24%
# （养老 16 + 医疗 7 + 失业 0.5 + 工伤 0.5），公积金 5%，福利/员工餐 2%。
# 三项合计 31%，落到 Y3 的"全口径人力成本率"≈26.6%，与 docs/01 §4 缺陷 2
# 给出的目标值 26.5% 一致 —— 这是独立复算出来的吻合，不是凑数。
SOCIAL_RATE, HOUSING_RATE, WELFARE_RATE = 0.24, 0.05, 0.02


def year_of(d: dt.date) -> int:
    if d.month >= 10:
        return {2023: 1, 2024: 2, 2025: 3, 2026: 4}.get(d.year, 4)
    return {2024: 1, 2025: 2, 2026: 3}.get(d.year, 3)


def plan_for(d: dt.date) -> dict:
    return PLAN[min(year_of(d), 3)]


def wait_ready(timeout: float = 90.0) -> None:
    """等后端真正能连上再开始灌。

    启动时 lifespan 会灌 dim_calendar（约 1,100 行），端口要过一会儿才 bind；
    不等的话第一个请求会 ConnectionRefused，整个脚本白跑。
    """
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            req("GET", "/api/health")
            print("后端已就绪")
            return
        except Exception as exc:          # noqa: BLE001 - 就绪探测要吞掉所有连接类异常
            last = str(exc)[:80]
            time.sleep(1.0)
    raise SystemExit(f"等后端就绪超时（{timeout}s）：{last}")


def main() -> None:
    wait_ready()
    shop = req("POST", "/api/shops", {
        "name": "社区快餐·测试店A",
        "category": "catering",
        "default_hourly_rate": 25,
        "note": "3 年合成测试数据（参数取自公开行业数据，非真实门店）",
    })
    sid = shop["id"]
    print(f"建店 id={sid}  name={shop['name']}")

    START, END = dt.date(2023, 10, 1), dt.date(2026, 10, 15)

    # ---- 逐日录入 ----
    d, n_day, n_shift = START, 0, 0
    while d <= END:
        p = plan_for(d)
        f = MONTH_F[d.month] * WEEK_F[d.weekday()] * (1.55 if d in HOLIDAYS else 1.0)
        rev = round(p["base"] * f * (1 + random.uniform(-0.08, 0.08)), 2)
        pur = round(rev * p["food"] * (1 + random.uniform(-0.03, 0.03)), 2)
        orders = max(1, round(rev / p["ticket"]))

        # M1 限制：labor_m 只能从排班现算，全职月薪按"月工资 ÷ (人数×8h×30天)"折算成时薪录入
        full_rate = round(p["full_monthly"] / (p["n_full"] * 8 * 30), 2)
        shifts = []
        for i in range(p["n_full"]):
            s, e = FULL_SLOTS[i]
            shifts.append({"worker_name": f"全职{chr(65 + i)}",
                           "start_time": s, "end_time": e, "hourly_rate": full_rate})
        shifts.append({"worker_name": "小时工甲", "start_time": "11:00",
                       "end_time": "14:00", "hourly_rate": p["part_rate"]})
        if d.weekday() in (5, 6) or d in HOLIDAYS:
            shifts.append({"worker_name": "小时工乙", "start_time": "17:00",
                           "end_time": "20:00", "hourly_rate": p["part_rate"]})

        req("PUT", "/api/daily", {
            "shop_id": sid, "stat_date": d.isoformat(),
            "values": {"revenue": rev, "purchase": pur, "orders": orders},
            "shifts": shifts,
        })
        n_day += 1
        n_shift += len(shifts)
        d += dt.timedelta(days=1)
    print(f"日录入 {n_day} 天，排班记录 {n_shift} 条")

    # ---- 逐月固定开销 ----
    months, m = [], dt.date(START.year, START.month, 1)
    while m <= END:
        months.append(f"{m.year:04d}-{m.month:02d}")
        m = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    for key in months:
        p = plan_for(dt.date(int(key[:4]), int(key[5:]), 1))
        # M2-1：全口径人工的月口径输入。
        #
        # ⚠ full_time_monthly **故意不填**（走 field_def 的 default_value=0）：
        #   本脚本把全职的工钱已经按"折算时薪"记进排班了（见上面 full_rate），
        #   再填一次月薪就是**重复计算**，labor_m_full 会虚高一倍。
        #   M2-1 真正补上的是 M1 里**根本没有位置**的那三项：社保 / 公积金 / 福利。
        #   字段本身可用性由 tests/test_daily.py 的单测证明（另一条录入约定：全职不走排班）。
        #
        # M3-2：五笔隐性成本（docs/03 §3）。它们的共同点是"老板本来就知道、只是没算进去"。
        h = p["hidden"]
        req("PUT", f"/api/shops/{sid}/monthly", {
            "month": key,
            "values": {
                "rent_m": p["rent"], "utility_m": p["util"],
                "gross_margin": round(1 - p["food"], 4),
                "full_time_headcount": p["n_full"],
                "social_insurance_m": round(p["full_monthly"] * SOCIAL_RATE),
                "housing_fund_m": round(p["full_monthly"] * HOUSING_RATE),
                "welfare_m": round(p["full_monthly"] * WELFARE_RATE),
                "staff_meal_m": h["staff_meal"],
                "tableware_m": h["tableware"],
                "repair_m": h["repair"],
                "cash_diff_m": h["cash_diff"],
                "returned_m": h["returned"],
            },
        })
    print(f"月固定开销 {len(months)} 个月（含 M2-1 社保/公积金/福利 + M3-2 隐性成本）")

    # ---- M4-5：商品主数据 + 按分类拆分的采购/入库 ----
    seed_goods_master(sid)
    listed = req("GET", f"/api/goods?shop_id={sid}&enabled_only=true")
    by_name = {(g["name"], g["spec"] or ""): g for g in listed}
    seed_category_purchases(sid, by_name, START, END)

    # ---- 三个经营年度报告 ----
    collect_reports(sid)


def seed_goods_master(sid: int) -> int:
    """播种商品主数据（M4-5）。

    数据表在 `server/goods_master.py`（服务端与脚本共用一份，避免两边漂移），
    但**写入仍然走 HTTP** —— 保持这个脚本"真实录入"的性质，不直接写库。
    分类主数据由服务启动时的 lifespan 播（全局共享，不带 shop_id）。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from server.goods_master import GOODS_SEEDS, ALIAS_SEEDS

    created: dict[tuple[str, str], dict] = {}
    for idx, row in enumerate(GOODS_SEEDS, start=1):
        (name, spec, cat2, stock_unit, buy_unit, conv, use_unit, s2u,
         price, weighed, abc) = row
        body = {
            "shop_id": sid,
            "name": name,
            "spec": spec,
            "spec_code": f"{(idx % 99) + 1:02d}",
            "category_code": cat2,
            "unit": stock_unit,
            "purchase_unit": buy_unit,
            "usage_unit": use_unit,
            "unit_conversion": conv,
            "stock_to_usage": s2u,
            "is_weighed": weighed,
            "unit_price": price,
            "abc_class": abc,
            # 种子数据是**设计好的主数据**，不是人在录，跳过查重弹窗
            "confirmed_not_duplicate": True,
        }
        goods = req("POST", "/api/goods", body)
        created[(name, spec)] = goods

    for alias, canonical in ALIAS_SEEDS:
        target = next((g for (n, _s), g in created.items() if n == canonical), None)
        if target is None:
            continue
        req("POST", f"/api/goods/{target['id']}/aliases", {"alias": alias, "note": "异名同物"})

    print(f"商品主数据 {len(created)} 个（含同料不同规格 {sum(1 for (n, s) in created if n == '大豆油')} 组、"
          f"别名 {len(ALIAS_SEEDS)} 条）")
    return len(created)


def seed_category_purchases(
    sid: int, by_name: dict[tuple[str, str], dict], start: dt.date, end: dt.date
) -> None:
    """把每天采购总额按**分类占比**拆开入库（M4-5）。

    设计意图：三年分类结构要变 —— **肉禽蛋占比逐年上升**，
    这样分类维度才能解释"食材成本率为什么从 30% 涨到 41%"，而不只是把数字拆开好看。

    做法：每天每个一级分类出一条采购 + 一条入库（在该分类里轮换具体商品）。
    一天 9 类 × 2 单 = 18 条，1111 天约 2 万条 —— 逐条 HTTP 跑不完，所以走批量口。
    """
    from server.goods_master import CATEGORY_SHARE_CURVE

    # 一级分类 → 该分类下的商品列表（用于轮换）
    bucket: dict[str, list[dict]] = {}
    for (name, spec), goods in by_name.items():
        major = (goods["category_code"] or "00").split("-")[0]
        bucket.setdefault(major, []).append(goods)
    for rows in bucket.values():
        rows.sort(key=lambda g: g["code"])

    d, n_purchase, n_receipt = start, 0, 0
    batch_p: list[dict] = []
    batch_r: list[dict] = []

    def flush() -> None:
        nonlocal n_purchase, n_receipt, batch_p, batch_r
        if batch_p:
            res = req("POST", f"/api/purchases/bulk?shop_id={sid}", batch_p)
            n_purchase += res["inserted"]
            if res["errors"]:
                print("  采购批量有问题：", res["errors"][:3])
        if batch_r:
            res = req("POST", f"/api/receipts/bulk?shop_id={sid}", batch_r)
            n_receipt += res["inserted"]
            if res["errors"]:
                print("  入库批量有问题：", res["errors"][:3])
        batch_p, batch_r = [], []

    while d <= end:
        p = plan_for(d)
        f = MONTH_F[d.month] * WEEK_F[d.weekday()] * (1.55 if d in HOLIDAYS else 1.0)
        day_total = p["base"] * f * p["food"]          # 当天食材成本总额（与日录入同源）
        year_idx = min(year_of(d), 3) - 1              # 0/1/2

        for major, shares in CATEGORY_SHARE_CURVE.items():
            rows = bucket.get(major)
            if not rows:
                continue
            amount = day_total * shares[year_idx]
            goods = rows[(d.toordinal() + int(major)) % len(rows)]
            # qty 按进货单位反推：amount ÷ (单价×换算率)
            unit_price = goods["unit_price"] * goods["unit_conversion"]
            qty = max(0.01, round(amount / unit_price, 3))
            item = {
                "shop_id": sid,
                "goods_id": goods["id"],
                "purchase_date": d.isoformat(),
                "qty": qty,
                "unit": goods["purchase_unit"] or goods["unit"],
                "unit_price": round(unit_price, 4),
            }
            batch_p.append(item)
            receipt = dict(item)
            if goods["is_weighed"]:
                # 称重品必须录实重：造一点自然损耗（-3%），这也是"账实差异"的现实来源
                receipt["actual_net_qty"] = round(qty * goods["unit_conversion"] * 0.97, 3)
            batch_r.append(receipt)

        if len(batch_p) >= 900:      # 约 50 天一批，控制单次请求体量
            flush()
        d += dt.timedelta(days=1)

    flush()
    print(f"分类采购 {n_purchase} 条 / 入库 {n_receipt} 条（按分类占比拆分，肉禽蛋占比逐年上升）")


PERIODS = [("Y1", "2023-10-01", "2024-09-30"),
           ("Y2", "2024-10-01", "2025-09-30"),
           ("Y3", "2025-10-01", "2026-09-30")]


def collect_reports(sid: int) -> None:
    """只重取三个年度报告并写回 JSON，**不重灌数据**。

    用途：改了基准区间 / 结论话术之后，只需要重新出一次报告就能验证，
    不必再花几分钟重灌 1,111 天（任务书 §0 要求"每改一次都要重跑验证"，
    重灌不该是唯一选项）。
    """
    out = {"shop_id": sid, "periods": {}}
    for name, s, e in PERIODS:
        r = req("GET", f"/api/report?shop_id={sid}&start={s}&end={e}")
        out["periods"][name] = {"start": s, "end": e, "report": r}
        print(f"\n===== {name}  {s} ~ {e} =====")
        print(json.dumps(r, ensure_ascii=False, indent=2)[:3000])

    with open("data/seed_3y_reports.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print("\n已写出 data/seed_3y_reports.json")
    print("\n已写出 data/seed_3y_reports.json")


if __name__ == "__main__":
    import sys

    # 只重取报告：python scripts/seed_3y_data.py --reports-only [shop_id]
    if "--reports-only" in sys.argv:
        idx = sys.argv.index("--reports-only")
        sid = int(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 else 1
        wait_ready()
        collect_reports(sid)
    else:
        main()
