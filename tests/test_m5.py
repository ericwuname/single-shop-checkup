"""M5 测试（docs/05）：BOM 让差异变纯 / 供应商应付 / 审批与原因代码化。

授权依据：`docs/05-M5长程施工任务书.md` §3 / §4 / §5 / §8。

每一组测试都对**门禁表里的一条判据**，注释里标了是第几条：

| 门禁 | 判据 | 测试 |
|---|---|---|
| 4 | 改口径后 `stock_variance_rate` **下降** + 量差/价差分别算出 | `test_*_variance_rate_drops_*` / `test_variance_*` |
| 5 | 88% 出成率 → 标准毛料 = 净料 ÷ 0.88 | `test_yield_rate_*` |
| 6 | 能说出"哪家逾期几天、欠多少" | `test_aging_sentence_*` |
| 7 | 超阈值无审批人 → 拒绝提交 | `test_approval_*` |
| 8 | 品牌色独立 | `scripts/ui_smoke.mjs` 第 8 组（真浏览器） |
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from server.models import Scrap

pytestmark = pytest.mark.usefixtures("session")


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _goods(client, shop, *, name="测试品", spec="散装", category_code="02-01", **extra) -> dict:
    body = {
        "shop_id": shop.id,
        "name": name,
        "spec": spec,
        "category_code": category_code,
        "confirmed_not_duplicate": True,
    }
    body.update(extra)
    res = client.post("/api/goods", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def _stocktake(client, shop, goods_id, *, on, actual_qty, **extra) -> dict:
    body = {
        "shop_id": shop.id,
        "goods_id": goods_id,
        "take_date": on,
        "actual_qty": actual_qty,
    }
    body.update(extra)
    res = client.put("/api/stocktakes", json=body)
    return res


def _report(client, shop, start="2026-03-01", end="2026-03-31") -> dict:
    return client.get(
        "/api/report",
        params={"shop_id": shop.id, "start": start, "end": end},
    ).json()


def _seed_period(client, shop_id, start_iso, days, *, revenue, food) -> None:
    """连续灌 N 天营业额/食材/排班（test_daily.py 的同名辅助在本文件的轻量版）。

    报损类指标需要营业额当分母，所以先灌几天真实口径的营业额。
    """
    from datetime import date, timedelta

    d0 = date.fromisoformat(start_iso)
    for i in range(days):
        res = client.put(
            "/api/daily",
            json={
                "shop_id": shop_id,
                "stat_date": (d0 + timedelta(days=i)).isoformat(),
                "values": {"revenue": revenue, "purchase": revenue * food},
                "shifts": [
                    {"worker_name": "全职A", "start_time": "09:00", "end_time": "17:00"},
                ],
            },
        )
        assert res.status_code in (200, 201), res.text


def _variance_rate(client, shop) -> float | None:
    data = _report(client, shop)
    return {m["code"]: m["value"] for m in data["metrics"]}["stock_variance_rate"]


# =========================================================================== #
# 门禁 5 · 出成率（v1.0 §3.2）
# =========================================================================== #
def test_yield_rate_gross_equals_net_divided_by_yield(client, shop):
    """门禁 5：出成率 88% → 标准毛料用量 = 净料 ÷ 0.88。

    不填出成率，标准成本必然算低 —— 采购按毛料付钱、BOM 按净料写量，
    毛净差是一笔永远对不上的账，最后全落在"差异"里说不清。
    """
    g = _goods(client, shop, name="土豆", unit="斤", unit_price=3.0)
    res = client.post(
        "/api/bom",
        json={"shop_id": shop.id, "dish_name": "酸辣土豆丝", "goods_id": g["id"],
              "std_qty_net": 10.0, "yield_rate": 0.88},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["std_qty_gross"] == pytest.approx(10.0 / 0.88, rel=1e-6)
    assert body["warning"] is None, "填了出成率就不该再告警"

    # 只算一次不够：要证明"毛 = 净 ÷ 出成率"这条关系在不同出成率下都成立
    res2 = client.post(
        "/api/bom",
        json={"shop_id": shop.id, "dish_name": "酸辣土豆丝", "goods_id": g["id"],
              "std_qty_net": 10.0, "yield_rate": 0.5},
    )
    assert res2.json()["std_qty_gross"] == pytest.approx(20.0)


def test_yield_rate_missing_warns_instead_of_silently_assuming(client, shop):
    """出成率没填 → 按 1.0 算，但**必须告警**（不能假装没损耗）。

    默认给 1.0 是"不评价"，不是"没损耗"。这个区别不说出来，
    老板会以为标准用量就是实际要买的量。
    """
    g = _goods(client, shop, name="白菜", unit="斤", unit_price=2.0)
    res = client.post(
        "/api/bom",
        json={"shop_id": shop.id, "dish_name": "手撕包菜", "goods_id": g["id"],
              "std_qty_net": 5.0},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["yield_rate"] == pytest.approx(1.0)
    assert body["std_qty_gross"] == pytest.approx(5.0)
    assert body["warning"] and "出成率" in body["warning"]


def test_bom_change_creates_new_version_not_overwrite(client, shop):
    """改配方 = 新版本，旧版本停用 —— 否则上个月的差异会跟当时算的不一样。"""
    g = _goods(client, shop, name="牛肉", unit="斤", unit_price=40.0)
    first = client.post(
        "/api/bom",
        json={"shop_id": shop.id, "dish_name": "水煮牛肉", "goods_id": g["id"],
              "std_qty_net": 3.0, "yield_rate": 1.0},
    ).json()
    assert first["version"] == 1

    second = client.post(
        "/api/bom",
        json={"shop_id": shop.id, "dish_name": "水煮牛肉", "goods_id": g["id"],
              "std_qty_net": 3.5, "yield_rate": 1.0},
    ).json()
    assert second["version"] == 2
    assert second["id"] != first["id"]

    rows = client.get("/api/bom", params={"shop_id": shop.id, "enabled_only": False}).json()
    assert len(rows) == 2, "旧版本必须留着（停用 ≠ 删除）"


# =========================================================================== #
# 门禁 4 · 量差 / 价差
# =========================================================================== #
def test_variance_qty_and_price_split_correctly(client, shop):
    """门禁 4：卖 100 份、标准 10kg、实际 11kg → 量差与价差分别正确，总和 = 两者之和。

    量差找厨房、价差找采购。**不拆就不知道该找谁** —— 总差异 500 元
    可能是"厨房浪费 500"，也可能是"鸡蛋涨价 500"。
    """
    g = _goods(client, shop, name="面粉", unit="kg", unit_price=20.0)
    # 每份 0.1kg × 100 份 = 理论 10kg
    res = client.post(
        "/api/bom",
        json={"shop_id": shop.id, "dish_name": "馒头", "goods_id": g["id"],
              "std_qty_net": 0.1, "yield_rate": 1.0, "std_price": 20.0},
    )
    assert res.status_code == 201, res.text

    res = client.post(
        "/api/bom/variance",
        json={"shop_id": shop.id, "biz_date": "2026-03-10", "dish_name": "馒头",
              "goods_id": g["id"], "sold_qty": 100, "actual_usage": 11.0,
              "actual_price": 22.0},
    )
    assert res.status_code == 201, res.text
    body = res.json()

    assert body["std_usage"] == pytest.approx(10.0)          # 100 × 0.1
    assert body["qty_variance"] == pytest.approx(1.0)        # 11 − 10
    assert body["qty_variance_amount"] == pytest.approx(20.0)    # 1kg × 20 元 = 找厨房
    assert body["price_variance_amount"] == pytest.approx(22.0)  # 2 元 × 11kg = 找采购

    summary = client.get(
        "/api/bom/variance/summary",
        params={"shop_id": shop.id, "start": "2026-03-01", "end": "2026-03-31"},
    ).json()
    assert summary["total_variance_amount"] == pytest.approx(42.0)
    assert summary["total_variance_amount"] == pytest.approx(
        summary["qty_variance_amount"] + summary["price_variance_amount"]
    ), "总差异必须等于量差 + 价差，不能各算各的"
    assert "厨房" in summary["conclusion"] or "采购" in summary["conclusion"]


def test_variance_needs_bom_first(client, shop):
    """没成本卡就算不出差异 —— 要明确报错，而不是默默按 0 算。"""
    g = _goods(client, shop, name="没有配方的料", unit="斤", unit_price=5.0)
    res = client.post(
        "/api/bom/variance",
        json={"shop_id": shop.id, "biz_date": "2026-03-10", "dish_name": "随便炒",
              "goods_id": g["id"], "sold_qty": 10, "actual_usage": 3.0},
    )
    assert res.status_code == 400
    assert "成本卡" in res.json()["detail"]


def test_variance_rate_drops_after_bom_usage_recorded(client, shop):
    """🔴 门禁 4 的核心：补上"领用"之后 `stock_variance_rate` 必须**下降**。

    造法：
      * 期初实盘 100 斤，期中入库 50 斤 → 账面 150
      * 期末实盘 120 斤 → 差 30 斤（20%）：这时系统以为"丢了 30 斤"
      * 登记"卖了 300 份 × 0.1 斤 = 30 斤"的理论领用 → 账面变 120，差 0
    """
    g = _goods(client, shop, name="白菜", unit="斤", unit_price=2.0)

    # 期初建账（没有上期实盘 → 不算盈亏，不用原因代码）
    assert _stocktake(client, shop, g["id"], on="2026-03-01", actual_qty=100).status_code == 200
    # 期中入库 50 斤
    rec = client.post(
        "/api/receipts",
        json={"shop_id": shop.id, "goods_id": g["id"], "purchase_date": "2026-03-05", "qty": 50},
    )
    assert rec.status_code == 201, rec.text

    # 期末盘点：账面 150 vs 实盘 120 → 差 30 斤（超 5% → 要原因 + 审批人）
    before = _stocktake(
        client, shop, g["id"], on="2026-03-31", actual_qty=120,
        reason_code="T08", approver="老板娘",
    )
    assert before.status_code == 200, before.text
    rate_before = _variance_rate(client, shop)
    assert rate_before == pytest.approx(30.0 / 150.0), f"改口径前应约 20%，实际 {rate_before}"

    # 登记理论领用 30 斤（卖 300 份 × 0.1 斤）
    bom = client.post(
        "/api/bom",
        json={"shop_id": shop.id, "dish_name": "手撕包菜", "goods_id": g["id"],
              "std_qty_net": 0.1, "yield_rate": 1.0, "std_price": 2.0},
    )
    assert bom.status_code == 201, bom.text
    var = client.post(
        "/api/bom/variance",
        json={"shop_id": shop.id, "biz_date": "2026-03-20", "dish_name": "手撕包菜",
              "goods_id": g["id"], "sold_qty": 300, "actual_usage": 30.0},
    )
    assert var.status_code == 201, var.text

    rate_after = _variance_rate(client, shop)
    assert rate_after is not None
    assert rate_after < rate_before, (
        f"补上领用后差异率必须下降：{rate_before} → {rate_after}"
    )
    assert rate_after == pytest.approx(0.0), "用掉的 30 斤被算进账面后，差异应该归零"


def test_book_formula_contains_theoretical_usage(client, shop):
    """账面公式里必须真的有"理论领用"这一项（不是只在文档里写了）。"""
    g = _goods(client, shop, name="大米", unit="斤", unit_price=3.0)
    assert _stocktake(client, shop, g["id"], on="2026-03-01", actual_qty=10).status_code == 200

    detail = client.get(
        "/api/stock/variance",
        params={"shop_id": shop.id, "start": "2026-03-01", "end": "2026-03-31"},
    ).json()
    reason = detail["items"][0]["book_reason"]
    assert reason["formula"] == "上期实盘 + Σ入库 − Σ理论领用 − Σ报损"
    assert "theoretical_usage_qty" in reason
    assert reason["missing"] is None, "M5-0 之后账面公式不再缺项"


# =========================================================================== #
# ABC 抽盘
# =========================================================================== #
def test_counting_due_uses_abc_cycle(client, shop):
    """A 每周 / B 每月 / C 每季 —— 只盘 A 类能抓 80% 差异。

    不降频的话老板第三天就放弃，差异分析也就落不了地。
    """
    a = _goods(client, shop, name="牛肉", abc_class="A", unit_price=40.0)
    b = _goods(client, shop, name="大米", abc_class="B", unit_price=3.0)
    c = _goods(client, shop, name="盐", abc_class="C", unit_price=1.0)

    # 全都盘过一次，但 A 类是 10 天前盘的最后一次 → 已到期
    assert _stocktake(client, shop, a["id"], on="2026-03-01", actual_qty=5).status_code == 200
    assert _stocktake(client, shop, b["id"], on="2026-03-01", actual_qty=5).status_code == 200
    assert _stocktake(client, shop, c["id"], on="2026-03-01", actual_qty=5).status_code == 200

    due = client.get(
        "/api/stock/counting-due",
        params={"shop_id": shop.id, "as_of": "2026-03-11"},
    ).json()
    by_id = {i["goods_id"]: i for i in due["items"]}

    assert by_id[a["id"]]["cycle_days"] == 7
    assert by_id[b["id"]]["cycle_days"] == 30
    assert by_id[c["id"]]["cycle_days"] == 90
    assert by_id[a["id"]]["due"] is True, "A 类 10 天没盘 → 到期"
    assert by_id[b["id"]]["due"] is False
    assert by_id[c["id"]]["due"] is False
    # A 类排最前 —— 老板打开就先看到该盘的
    assert due["items"][0]["goods_id"] == a["id"]


# =========================================================================== #
# 门禁 7 · 审批与原因代码化
# =========================================================================== #
def test_scrap_over_threshold_without_approver_is_rejected(client, shop):
    """门禁 7：金额与比例**都**过线、无审批人 → 拒绝提交。

    ⚠️ M6 改「且」后这里补了一笔采购当比例分母：600 元报废要有分母才能算比例。
    没有采购数据时比例算不出来，只按金额闸门判 —— 那是"宁可松"的设计。
    """
    g = _goods(client, shop, name="牛肉", unit="斤", unit_price=60.0)
    buy = client.post(
        "/api/purchases",
        json={"shop_id": shop.id, "goods_id": g["id"], "purchase_date": "2026-03-02",
              "qty": 10, "unit_price": 60.0},
    )
    assert buy.status_code == 201, buy.text
    res = client.post(
        "/api/scraps",
        json={"shop_id": shop.id, "goods_id": g["id"], "scrap_date": "2026-03-10",
              "qty": 10, "reason_code": "T02"},           # 600 元 ÷ 600 元 = 100%
    )
    assert res.status_code == 400
    assert "审批" in res.json()["detail"]

    ok = client.post(
        "/api/scraps",
        json={"shop_id": shop.id, "goods_id": g["id"], "scrap_date": "2026-03-10",
              "qty": 10, "reason_code": "T02", "approver": "老板娘"},
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["approver"] == "老板娘"


def test_scrap_small_amount_but_huge_ratio_needs_approval(client, shop):
    """⚠️ **M6 改了语义**（`docs/06-M6` §0 裁定 2，门禁 3）。

    这条测试在 M5 写的是"「或」是关键：小额但比例 90% → 同样要签字"。
    现在阈值关系改成「**且**」——金额是主闸门、比例只兜底。
    所以这条从"必须拒"翻转成"**金额没过线就不拒**"，
    并新加 `test_approval_threshold_requires_both_amount_and_rate` 做门禁 3 的双向断言。

    **不是删了这条**，是把"或"时代的判据换成"且"时代的判据 —— 语义翻转必须显式留痕。
    比例的分母仍然是**当月进了多少货**（损耗率 = 报损 ÷ 进货），这一点没变。
    """
    g = _goods(client, shop, name="虫草花", unit="斤", unit_price=50.0)
    buy = client.post(
        "/api/purchases",
        json={"shop_id": shop.id, "goods_id": g["id"], "purchase_date": "2026-03-02",
              "qty": 1, "unit_price": 50.0},
    )
    assert buy.status_code == 201, buy.text

    res = client.post(
        "/api/scraps",
        json={"shop_id": shop.id, "goods_id": g["id"], "scrap_date": "2026-03-10",
              "qty": 0.9, "reason_code": "T02"},         # 45 元 ÷ 50 元 = 90%
    )
    assert res.status_code == 201, (
        "「且」时代：金额没过 100 元线就不该要签字 —— "
        "餐饮损耗率 5–10% 是常态，按比例单独闸门等于天天签字"
    )


def test_scrap_without_purchase_base_only_uses_amount_threshold(client, shop):
    """没有进货数据时比例算不出来 → **只按 100 元判断**，不能拿个假分母算出 100%。"""
    g = _goods(client, shop, name="没进过货的东西", unit="斤", unit_price=50.0)
    res = client.post(
        "/api/scraps",
        json={"shop_id": shop.id, "goods_id": g["id"], "scrap_date": "2026-03-10",
              "qty": 0.9, "reason_code": "T02"},         # 45 元，无分母
    )
    assert res.status_code == 201, res.text


def test_scrap_reason_must_be_code_not_free_text(client, shop):
    """损耗原因必须代码化：写"坏了"要被拒，写 T02 才行。

    自由文本不能聚合 —— 报表明明有数据却回答不了"损耗主要发生在哪一环"。
    """
    g = _goods(client, shop, name="生菜", unit="斤", unit_price=4.0)
    bad = client.post(
        "/api/scraps",
        json={"shop_id": shop.id, "goods_id": g["id"], "scrap_date": "2026-03-10",
              "qty": 1, "reason": "坏了"},
    )
    assert bad.status_code == 400
    assert "T01" in bad.json()["detail"], "报错里要给他可选的码，不然他不知道填什么"

    worse = client.post(
        "/api/scraps",
        json={"shop_id": shop.id, "goods_id": g["id"], "scrap_date": "2026-03-10",
              "qty": 1, "reason_code": "T99"},
    )
    assert worse.status_code == 400, "不在册的代码也要拒，不能悄悄存成自由文本"

    good = client.post(
        "/api/scraps",
        json={"shop_id": shop.id, "goods_id": g["id"], "scrap_date": "2026-03-10",
              "qty": 1, "reason_code": "t02"},           # 小写也要收（老板手打不会大写）
    )
    assert good.status_code == 201, good.text
    assert good.json()["reason_code"] == "T02"


def test_loss_reason_breakdown_aggregates(client, shop):
    """代码化换来的东西：能回答"损耗主要发生在哪一环"。"""
    g = _goods(client, shop, name="鸡蛋", unit="斤", unit_price=10.0)
    for code, qty in (("T01", 1.0), ("T01", 2.0), ("T02", 5.0)):
        res = client.post(
            "/api/scraps",
            json={"shop_id": shop.id, "goods_id": g["id"], "scrap_date": "2026-03-10",
                  "qty": qty, "reason_code": code},
        )
        assert res.status_code == 201, res.text

    rows = client.get(
        "/api/scraps/breakdown",
        params={"shop_id": shop.id, "start": "2026-03-01", "end": "2026-03-31"},
    ).json()
    by_code = {r["code"]: r for r in rows}
    assert by_code["T02"]["amount"] == pytest.approx(50.0)
    assert by_code["T01"]["count"] == 2
    assert rows[0]["code"] == "T02", "金额大的排前面"
    assert rows[0]["share"] == pytest.approx(50.0 / 80.0)

    dictionary = client.get("/api/scraps/reasons").json()
    assert {d["code"] for d in dictionary} >= {"T01", "T02", "T08"}


def test_stocktake_over_threshold_needs_approver(client, shop):
    """盘亏超阈值没有审批人 → 拒绝；有审批人 → 收。"""
    g = _goods(client, shop, name="白酒", unit="瓶", unit_price=200.0)
    assert _stocktake(client, shop, g["id"], on="2026-03-01", actual_qty=100).status_code == 200

    # ⚠️ M6 改「且」后把差额从 5 瓶调到 10 瓶：原来 5/100 = 5% 刚好卡在阈值上
    # （不 > 5%），"且"语义下就不触发了。10/100 = 10% > 5% 才够。
    bad = _stocktake(
        client, shop, g["id"], on="2026-03-31", actual_qty=90, reason_code="T08"
    )                                                    # 差 10 瓶 = 2000 元 / 10%
    assert bad.status_code == 400
    assert "审批" in bad.json()["detail"]

    ok = _stocktake(
        client, shop, g["id"], on="2026-03-31", actual_qty=90,
        reason_code="T08", approver="老板娘",
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["reason_name"] == "盘亏"


def test_opening_stocktake_is_not_treated_as_gain(client, shop):
    """第一次录库存（没有上期实盘）不是"盘盈"，不该问原因、不该要审批。

    老板第一天把货录进来，账面从 0 起算，差异 = 全部库存。
    对建账要签字是荒谬的 —— 这个坑是写测试时才发现的（M4 那条测试挂在这里）。
    """
    g = _goods(client, shop, name="油", unit="L", unit_price=10.0)
    res = _stocktake(client, shop, g["id"], on="2026-03-01", actual_qty=500)   # 5000 元
    assert res.status_code == 200, res.text
    assert res.json()["reason_code"] is None


def test_purchase_discount_over_threshold_needs_approver(client, shop):
    """折价审批：供应商让掉 500 元也得有人签字。"""
    g = _goods(client, shop, name="海鲜", unit="斤", unit_price=100.0)
    bad = client.post(
        "/api/purchases",
        json={"shop_id": shop.id, "goods_id": g["id"], "purchase_date": "2026-03-05",
              "qty": 10, "discount_amount": 500},
    )
    assert bad.status_code == 400
    assert "审批" in bad.json()["detail"]

    ok = client.post(
        "/api/purchases",
        json={"shop_id": shop.id, "goods_id": g["id"], "purchase_date": "2026-03-05",
              "qty": 10, "discount_amount": 500, "approver": "老板娘"},
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["discount_amount"] == pytest.approx(500.0)


# =========================================================================== #
# 门禁 6 · 供应商与应付账龄
# =========================================================================== #
def _supplier(client, shop, name, *, credit_days=0, **extra) -> dict:
    body = {"shop_id": shop.id, "name": name, "credit_days": credit_days}
    body.update(extra)
    res = client.post("/api/suppliers", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def test_aging_sentence_names_days_left_and_overdue(client, shop):
    """门禁 6 验收原话：「欠 3 家共 X 元，A 家还有 5 天到期，C 家已逾期 12 天」。"""
    a = _supplier(client, shop, "甲蔬菜", credit_days=30)
    b = _supplier(client, shop, "乙肉类", credit_days=15)
    c = _supplier(client, shop, "丙粮油", credit_days=7)

    as_of = date(2026, 3, 20)
    # A：还有 5 天到期（03-25）
    assert client.post("/api/payables", json={
        "shop_id": shop.id, "supplier_id": a["id"], "biz_date": "2026-02-23",
        "due_date": (as_of + timedelta(days=5)).isoformat(), "amount": 3000,
    }).status_code == 201
    # B：还有 20 天
    assert client.post("/api/payables", json={
        "shop_id": shop.id, "supplier_id": b["id"], "biz_date": "2026-03-01",
        "due_date": (as_of + timedelta(days=20)).isoformat(), "amount": 5000,
    }).status_code == 201
    # C：已逾期 12 天
    assert client.post("/api/payables", json={
        "shop_id": shop.id, "supplier_id": c["id"], "biz_date": "2026-03-01",
        "due_date": (as_of - timedelta(days=12)).isoformat(), "amount": 2000,
    }).status_code == 201

    aging = client.get(
        "/api/payables/aging",
        params={"shop_id": shop.id, "as_of": as_of.isoformat()},
    ).json()
    sentence = aging["sentence"]
    assert sentence is not None
    assert "欠 3 家共 10,000 元" in sentence
    assert "甲蔬菜还有 5 天到期" in sentence
    assert "丙粮油已逾期 12 天" in sentence

    assert aging["outstanding_total"] == pytest.approx(10000.0)
    assert aging["overdue_total"] == pytest.approx(2000.0)
    assert aging["overdue_count"] == 1
    # 账龄分桶（M5 自查 A4 改过）：**未到期必须单独一档**，不能混进 1-30
    buckets = {b["bucket"]: b["amount"] for b in aging["aging"]}
    assert buckets["未到期"] == pytest.approx(8000.0)
    assert buckets["1-30"] == pytest.approx(2000.0)


def test_aging_separates_not_yet_due_from_overdue(client, shop):
    """审查项 A4 的核心：同样是"欠 1 万"，全未到期 vs 全逾期 20 天，必须看得出区别。

    早先把未到期塞进 0-30，这两种情况长得一模一样 —— 报表就失去了指向性。
    """
    s = _supplier(client, shop, "供应商甲", credit_days=30)
    as_of = date(2026, 3, 31)
    # 情形 A：全部还没到期
    for i in range(2):
        assert client.post("/api/payables", json={
            "shop_id": shop.id, "supplier_id": s["id"], "biz_date": "2026-03-01",
            "due_date": (as_of + timedelta(days=10)).isoformat(), "amount": 5000,
        }).status_code == 201
    a = client.get("/api/payables/aging",
                   params={"shop_id": shop.id, "as_of": as_of.isoformat()}).json()
    assert {b["bucket"]: b["amount"] for b in a["aging"]}["未到期"] == pytest.approx(10000.0)
    assert a["overdue_total"] == pytest.approx(0.0)

    # 情形 B：全部已逾期 20 天（先清库再建）
    for row in client.get("/api/payables", params={"shop_id": shop.id}).json():
        client.post(f"/api/payables/{row['id']}/pay", params={"amount": 99999})
    for i in range(2):
        assert client.post("/api/payables", json={
            "shop_id": shop.id, "supplier_id": s["id"], "biz_date": "2026-03-01",
            "due_date": (as_of - timedelta(days=20)).isoformat(), "amount": 5000,
        }).status_code == 201
    b = client.get("/api/payables/aging",
                   params={"shop_id": shop.id, "as_of": as_of.isoformat()}).json()
    assert {x["bucket"]: x["amount"] for x in b["aging"]}["未到期"] == pytest.approx(0.0)
    assert {x["bucket"]: x["amount"] for x in b["aging"]}["1-30"] == pytest.approx(10000.0)
    assert b["overdue_total"] == pytest.approx(10000.0)


def test_aging_buckets_split_by_overdue_days(client, shop):
    """账龄按**逾期天数**分桶：未到期 / 1-30 / 31-60 / 61-90 / 90+。"""
    s = _supplier(client, shop, "老供应商", credit_days=30)
    as_of = date(2026, 6, 30)
    for days in (10, 45, 70, 120):
        assert client.post("/api/payables", json={
            "shop_id": shop.id, "supplier_id": s["id"], "biz_date": "2026-01-01",
            "due_date": (as_of - timedelta(days=days)).isoformat(), "amount": 1000,
        }).status_code == 201

    aging = client.get(
        "/api/payables/aging",
        params={"shop_id": shop.id, "as_of": as_of.isoformat()},
    ).json()
    buckets = {b["bucket"]: b["amount"] for b in aging["aging"]}
    assert buckets["未到期"] == pytest.approx(0.0)
    assert buckets["1-30"] == pytest.approx(1000.0)
    assert buckets["31-60"] == pytest.approx(1000.0)
    assert buckets["61-90"] == pytest.approx(1000.0)
    assert buckets["90+"] == pytest.approx(1000.0)
    assert aging["overdue_total"] == pytest.approx(4000.0)


def test_paid_payable_disappears_from_outstanding(client, shop):
    """付清之后不再进账龄 —— 否则老板会以为自己还欠钱。"""
    s = _supplier(client, shop, "现金供应商", credit_days=7)
    p = client.post("/api/payables", json={
        "shop_id": shop.id, "supplier_id": s["id"], "biz_date": "2026-03-01",
        "due_date": "2026-03-08", "amount": 800,
    }).json()
    assert p["outstanding"] == pytest.approx(800.0)

    paid = client.post(
        f"/api/payables/{p['id']}/pay", params={"amount": 800}
    ).json()
    assert paid["outstanding"] == pytest.approx(0.0)

    aging = client.get(
        "/api/payables/aging",
        params={"shop_id": shop.id, "as_of": "2026-03-20"},
    ).json()
    assert aging["outstanding_total"] == pytest.approx(0.0)
    assert aging["sentence"] is None, "都结清了就别再说「欠 X 家」"


def test_credit_purchase_auto_creates_payable(client, shop):
    """挂账供应商（credit_days>0）的采购自动生成应付 —— 不用老板再记一遍。"""
    s = _supplier(client, shop, "赊账供应商", credit_days=15)
    g = _goods(client, shop, name="大米", unit="斤", unit_price=3.0)
    res = client.post(
        "/api/purchases",
        json={"shop_id": shop.id, "goods_id": g["id"], "purchase_date": "2026-03-01",
              "qty": 100, "unit_price": 3.0, "supplier_id": s["id"]},
    )
    assert res.status_code == 201, res.text
    assert res.json()["supplier_name"] == "赊账供应商"

    rows = client.get("/api/payables", params={"shop_id": shop.id}).json()
    assert len(rows) == 1
    assert rows[0]["due_date"] == "2026-03-16"        # 03-01 + 15 天
    assert rows[0]["amount"] == pytest.approx(300.0)


def test_cash_purchase_creates_no_payable(client, shop):
    """现结（credit_days=0）不该凭空生成应付。"""
    s = _supplier(client, shop, "现结供应商", credit_days=0)
    g = _goods(client, shop, name="青菜", unit="斤", unit_price=2.0)
    assert client.post(
        "/api/purchases",
        json={"shop_id": shop.id, "goods_id": g["id"], "purchase_date": "2026-03-01",
              "qty": 10, "supplier_id": s["id"]},
    ).status_code == 201
    assert client.get("/api/payables", params={"shop_id": shop.id}).json() == []


def test_supplier_rating_uses_documented_weights(client, shop):
    """评级照抄 v0.2 §3.1.1：准时 30 / 质量 30 / 价格稳定 20 / 损耗 20。"""
    g = _goods(client, shop, name="猪肉", unit="斤", unit_price=20.0)
    # 完美供应商：10 次全准时、10 批全合格、价格一条（稳定）、无报损
    perfect = _supplier(client, shop, "A级供应商", credit_days=10,
                        on_time_deliveries=10, total_deliveries=10,
                        qualified_batches=10, total_batches=10)
    for i in range(3):
        assert client.post(
            "/api/purchases",
            json={"shop_id": shop.id, "goods_id": g["id"],
                  "purchase_date": f"2026-03-0{i + 1}", "qty": 10,
                  "unit_price": 20.0, "supplier_id": perfect["id"]},
        ).status_code == 201

    ratings = client.get(
        "/api/suppliers/ratings",
        params={"shop_id": shop.id, "start": "2026-03-01", "end": "2026-03-31"},
    ).json()
    assert len(ratings) == 1
    top = ratings[0]
    assert top["grade"] == "A", f"全满分应该是 A，实际 {top['score']} / {top['grade']}"
    assert top["score"] == pytest.approx(100.0)
    assert set(top["breakdown"]) == {"on_time", "quality", "price_stable", "loss"}


def test_new_supplier_is_not_punished_for_missing_data(client, shop):
    """没有配送记录 → 按"不评价"给满分，不是给 0 分把新供应商打成 D。

    "不知道"和"很差"必须分开表达 —— 否则老板会以为这家很差，其实只是没数据。
    """
    s = _supplier(client, shop, "刚建档的供应商")
    ratings = client.get(
        "/api/suppliers/ratings",
        params={"shop_id": shop.id, "start": "2026-03-01", "end": "2026-03-31"},
    ).json()
    row = next(r for r in ratings if r["supplier_id"] == s["id"])
    assert row["breakdown"]["on_time"] == pytest.approx(1.0)
    assert row["breakdown"]["quality"] == pytest.approx(1.0)
    assert row["grade"] in ("A", "B"), f"没数据不该被打成 D，实际 {row['grade']}"


def test_supplier_concentration_top1_share(client, shop):
    """集中度：TOP1 占比。太集中断供风险大。"""
    g = _goods(client, shop, name="米面油", unit="斤", unit_price=5.0)
    s1 = _supplier(client, shop, "大头")
    s2 = _supplier(client, shop, "小头")
    for sid, qty in ((s1["id"], 90), (s2["id"], 10)):
        assert client.post(
            "/api/purchases",
            json={"shop_id": shop.id, "goods_id": g["id"], "purchase_date": "2026-03-01",
                  "qty": qty, "unit_price": 5.0, "supplier_id": sid},
        ).status_code == 201

    con = client.get(
        "/api/suppliers/concentration",
        params={"shop_id": shop.id, "start": "2026-03-01", "end": "2026-03-31"},
    ).json()
    assert con["top1_share"] == pytest.approx(0.9)
    assert con["items"][0]["supplier_name"] == "大头"
    assert "断供" in con["note"], "占比 90% 必须提示断供风险"


def test_duplicate_supplier_name_rejected(client, shop):
    """同名供应商建两份 → 以后的评级和账龄都会被摊薄，直接拒。"""
    _supplier(client, shop, "同名供应商")
    res = client.post("/api/suppliers", json={"shop_id": shop.id, "name": "同名供应商"})
    assert res.status_code == 409


# =========================================================================== #
# 回归：M4 修过的端点重复注册问题
# =========================================================================== #
def test_openapi_has_no_duplicate_operation_ids(client):
    """M4 踩过的坑：`/api/shops` 被两个路由文件注册，FastAPI 只是**警告**不报错，
    结果后注册的那个被静默覆盖。这里把警告当错误，防止再犯。
    """
    spec = client.get("/openapi.json").json()
    ids = [
        op.get("operationId")
        for path in spec["paths"].values()
        for op in path.values()
        if isinstance(op, dict) and op.get("operationId")
    ]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"operationId 重复（会被静默覆盖）：{dupes}"


# =========================================================================== #
# 报告层：门禁 6 的落点（"报告能说出"）
# =========================================================================== #
def test_report_observations_speak_payables(client, shop):
    """门禁 6 的"报告"两个字是实的：账龄那句话要出现在 /api/report 的 observations 里。

    为什么不塞进结论三行：结论严格三行是门禁（verify_m2 有断言），
    而且"欠谁钱"是要单独看的一块，塞进去会挤掉更重要的那句话。
    """
    a = _supplier(client, shop, "甲蔬菜", credit_days=30)
    c = _supplier(client, shop, "丙粮油", credit_days=7)
    assert client.post("/api/payables", json={
        "shop_id": shop.id, "supplier_id": a["id"], "biz_date": "2026-02-23",
        "due_date": "2026-03-25", "amount": 3000,
    }).status_code == 201
    assert client.post("/api/payables", json={
        "shop_id": shop.id, "supplier_id": c["id"], "biz_date": "2026-03-01",
        "due_date": "2026-03-08", "amount": 2000,
    }).status_code == 201

    data = _report(client, shop, start="2026-03-01", end="2026-03-20")
    pay = data["observations"]["payables"]
    assert pay["sentence"] and "欠 2 家共 5,000 元" in pay["sentence"]
    assert "甲蔬菜还有 5 天到期" in pay["sentence"]
    assert "丙粮油已逾期 12 天" in pay["sentence"]
    # 结论仍然严格三行 —— 这块数据不许挤掉结论
    assert len(data["conclusion"]["lines"]) == 3


def test_report_observations_speak_bom_variance(client, shop):
    """量差/价差也进报告 observations（量差找厨房、价差找采购）。"""
    g = _goods(client, shop, name="鸡蛋", unit="斤", unit_price=10.0)
    assert client.post(
        "/api/bom",
        json={"shop_id": shop.id, "dish_name": "番茄炒蛋", "goods_id": g["id"],
              "std_qty_net": 0.2, "yield_rate": 1.0, "std_price": 10.0},
    ).status_code == 201
    assert client.post(
        "/api/bom/variance",
        json={"shop_id": shop.id, "biz_date": "2026-03-10", "dish_name": "番茄炒蛋",
              "goods_id": g["id"], "sold_qty": 100, "actual_usage": 25.0,
              "actual_price": 12.0},
    ).status_code == 201

    data = _report(client, shop)
    bom = data["observations"]["bom_variance"]
    assert bom["has_data"] is True
    assert bom["qty_variance_amount"] == pytest.approx(50.0)     # (25−20) × 10
    assert bom["price_variance_amount"] == pytest.approx(50.0)   # (12−10) × 25
    assert "厨房" in bom["conclusion"] or "采购" in bom["conclusion"]


# =========================================================================== #
# 自查审查补的测试（docs/06）
# =========================================================================== #
def test_approval_and_report_use_the_same_book_qty(client, shop):
    """审查项 C2 / E2（**阻断**）：签字时算的差异，必须和报告里显示的是同一个数。

    早先路由里自己写了一份"老板自报优先"，而 stock_service 是"系统推算优先"——
    同一张盘点单在两个地方算出不同的差异，老板等于在签一个他看不见的数。
    """
    g = _goods(client, shop, name="大米", unit="斤", unit_price=10.0)
    # 期初 100
    assert _stocktake(client, shop, g["id"], on="2026-03-01", actual_qty=100).status_code == 200
    # 入库 50 → 系统推算账面 = 150
    rec = client.post(
        "/api/receipts",
        json={"shop_id": shop.id, "goods_id": g["id"], "purchase_date": "2026-03-05", "qty": 50},
    )
    assert rec.status_code == 201, rec.text

    # 老板自己记的账是 100（少记了），实盘 120
    res = _stocktake(
        client, shop, g["id"], on="2026-03-31", actual_qty=120,
        book_qty=100, reason_code="T08", approver="老板娘",
    )
    assert res.status_code == 200, res.text

    detail = client.get(
        "/api/stock/variance",
        params={"shop_id": shop.id, "start": "2026-03-01", "end": "2026-03-31"},
    ).json()
    item = detail["items"][-1]
    # 报告用系统推算的 150（不是老板自报的 100）—— 判定也必须用这一个
    assert item["book_qty"] == pytest.approx(150.0)
    assert item["variance_qty"] == pytest.approx(-30.0)
    # 反证：如果还按老板自报的 100 算，差异会是 +20，这里必须不是 +20
    assert item["variance_qty"] != pytest.approx(20.0)


def test_bom_unit_must_match_goods_stock_unit(client, shop):
    """审查项 E1（必修）：成本卡用量单位 ≠ 商品库存单位 → **拒绝**，不能悄悄存。

    行业把"采购单位 + 核算单位"都列为标准成本卡必备字段，理由是
    "系统按干品计、厨房按泡发后计，理论成本和实际领料必然无法对齐"。
    我们的理论领用要直接减进账面库存，单位错配就是两个单位做减法。
    """
    g = _goods(client, shop, name="木耳", unit="斤", unit_price=30.0)   # 库存单位：斤
    ok = client.post(
        "/api/bom",
        json={"shop_id": shop.id, "dish_name": "凉拌木耳", "goods_id": g["id"],
              "std_qty_net": 0.1, "yield_rate": 1.0, "unit": "斤"},
    )
    assert ok.status_code == 201, ok.text

    bad = client.post(
        "/api/bom",
        json={"shop_id": shop.id, "dish_name": "凉拌木耳", "goods_id": g["id"],
              "std_qty_net": 0.1, "yield_rate": 1.0, "unit": "袋"},
    )
    assert bad.status_code == 400
    assert "库存单位" in bad.json()["detail"]


def test_std_cost_shown_for_boss_to_verify(client, shop):
    """审查项 A3：标准成本 = 毛料用量 × 毛料价，显式给出让老板能核对。

    行业口径是 净料单价 = 毛料单价 ÷ 净料率。我们存毛料价配毛料用量，
    两者相乘才自洽 —— 这个数要给出来，不然老板没法判断配方录得对不对。
    """
    g = _goods(client, shop, name="牛肉", unit="斤", unit_price=40.0)
    res = client.post(
        "/api/bom",
        json={"shop_id": shop.id, "dish_name": "水煮牛肉", "goods_id": g["id"],
              "std_qty_net": 0.25, "yield_rate": 0.8, "std_price": 40.0},
    ).json()
    assert res["std_qty_gross"] == pytest.approx(0.25 / 0.8)     # 0.3125
    assert res["std_cost"] == pytest.approx(0.3125 * 40.0)       # 12.5 元/份


def test_actual_usage_can_be_derived_from_stocktakes(client, shop):
    """审查项 F2（必修）：实际用量**可以不录**，系统从盘点反推 —— 能算的别让老板填。

    反推公式：实际耗用 = 期初实盘 + Σ入库 − 期末实盘 − Σ报损
    ⚠️ 不能用"账面"反推：账面已经减掉理论领用了，会自我循环。
    """
    g = _goods(client, shop, name="面粉", unit="kg", unit_price=5.0)
    assert client.post(
        "/api/bom",
        json={"shop_id": shop.id, "dish_name": "馒头", "goods_id": g["id"],
              "std_qty_net": 0.1, "yield_rate": 1.0, "std_price": 5.0},
    ).status_code == 201

    # 期初 100 kg → 入库 20 kg → 期末 90 kg → 报损 5 kg ⇒ 实际耗用 = 100+20−90−5 = 25
    assert _stocktake(client, shop, g["id"], on="2026-03-01", actual_qty=100).status_code == 200
    assert client.post(
        "/api/receipts",
        json={"shop_id": shop.id, "goods_id": g["id"], "purchase_date": "2026-03-05", "qty": 20},
    ).status_code == 201
    assert client.post(
        "/api/scraps",
        json={"shop_id": shop.id, "goods_id": g["id"], "scrap_date": "2026-03-06",
              "qty": 5, "reason_code": "T02"},
    ).status_code == 201
    # 期末盘点：系统推算账面 = 100+20−5 = 115，实盘 90 → 差 125 元 > 100 元 → 要签字
    last = _stocktake(client, shop, g["id"], on="2026-03-10", actual_qty=90,
                      reason_code="T08", approver="老板娘")
    assert last.status_code == 200, last.text
    assert last.json()["computed_book_qty"] == pytest.approx(115.0)

    # 只录卖出份数，实际用量留空
    res = client.post(
        "/api/bom/variance",
        json={"shop_id": shop.id, "biz_date": "2026-03-10", "dish_name": "馒头",
              "goods_id": g["id"], "sold_qty": 200},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["actual_usage"] == pytest.approx(25.0)
    assert body["std_usage"] == pytest.approx(20.0)         # 200 × 0.1
    assert body["qty_variance_amount"] == pytest.approx(25.0)   # 多用 5kg × 5 元


def test_variance_without_stocktake_data_asks_boss_to_fill(client, shop):
    """反推不出来（盘点不足两次）→ 明确报错，不是默默按 0 算。"""
    g = _goods(client, shop, name="没盘过的货", unit="斤", unit_price=6.0)
    assert client.post(
        "/api/bom",
        json={"shop_id": shop.id, "dish_name": "随便", "goods_id": g["id"],
              "std_qty_net": 0.1, "yield_rate": 1.0},
    ).status_code == 201
    res = client.post(
        "/api/bom/variance",
        json={"shop_id": shop.id, "biz_date": "2026-03-10", "dish_name": "随便",
              "goods_id": g["id"], "sold_qty": 10},
    )
    assert res.status_code == 400
    assert "盘点" in res.json()["detail"]


# =========================================================================== #
# 第二轮自查补的测试（docs/07）
# =========================================================================== #
def test_derived_usage_uses_last_two_stocktakes_only(client, shop):
    """审查项 G2（**必修**）：反推耗用的期初必须是**上一次**盘点，不是最早一次。

    三次盘点的场景：3-1 / 3-10 / 3-20。
    3-20 录差异时，如果期初取 3-1，算出来的就是"3-1~3-20 累计耗用"，
    却拿去和"3-20 单日理论耗用"比 —— 量差虚高，且前期已算过的又算一遍。
    """
    g = _goods(client, shop, name="白菜", unit="斤", unit_price=2.0)
    assert client.post(
        "/api/bom",
        json={"shop_id": shop.id, "dish_name": "手撕包菜", "goods_id": g["id"],
              "std_qty_net": 0.1, "yield_rate": 1.0, "std_price": 2.0},
    ).status_code == 201

    assert _stocktake(client, shop, g["id"], on="2026-03-01", actual_qty=100).status_code == 200
    assert client.post(
        "/api/receipts",
        json={"shop_id": shop.id, "goods_id": g["id"], "purchase_date": "2026-03-02", "qty": 10},
    ).status_code == 201
    assert _stocktake(client, shop, g["id"], on="2026-03-10", actual_qty=80,
                      reason_code="T08", approver="老板娘").status_code == 200
    # 3-10 到 3-20 之间：入库 5，期末 60 ⇒ 实际耗用 = 80 + 5 − 60 = 25
    assert client.post(
        "/api/receipts",
        json={"shop_id": shop.id, "goods_id": g["id"], "purchase_date": "2026-03-15", "qty": 5},
    ).status_code == 201
    last = _stocktake(client, shop, g["id"], on="2026-03-20", actual_qty=60,
                      reason_code="T08", approver="老板娘")
    assert last.status_code == 200, last.text

    res = client.post(
        "/api/bom/variance",
        json={"shop_id": shop.id, "biz_date": "2026-03-20", "dish_name": "手撕包菜",
              "goods_id": g["id"], "sold_qty": 200},
    )
    assert res.status_code == 201, res.text
    # 若按最早那次（3-1 = 100）算，会得到 100+15−60 = 55；正确是 25
    assert res.json()["actual_usage"] == pytest.approx(25.0)
    assert res.json()["actual_usage"] != pytest.approx(55.0)


def test_duplicate_variance_submission_overwrites_not_doubles(client, shop):
    """审查项 G3（**必修**）：同一天同一菜同一料重复提交 → 覆盖，不是再加一条。

    `std_usage` 直接减进**账面库存**。不覆盖的话，老板手抖连点两次，
    账面凭空少掉两份料、差异率虚高 —— 而且数字看着都"正常"，极难发现。
    """
    g = _goods(client, shop, name="面粉", unit="kg", unit_price=5.0)
    assert client.post(
        "/api/bom",
        json={"shop_id": shop.id, "dish_name": "馒头", "goods_id": g["id"],
              "std_qty_net": 0.1, "yield_rate": 1.0, "std_price": 5.0},
    ).status_code == 201
    assert _stocktake(client, shop, g["id"], on="2026-03-01", actual_qty=100).status_code == 200
    assert _stocktake(client, shop, g["id"], on="2026-03-05", actual_qty=80,
                      reason_code="T08", approver="老板娘").status_code == 200

    body = {"shop_id": shop.id, "biz_date": "2026-03-05", "dish_name": "馒头",
            "goods_id": g["id"], "sold_qty": 100}
    first = client.post("/api/bom/variance", json=body)
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]

    # 老板手抖，又点了一次（份数改对了）
    body2 = dict(body, sold_qty=120)
    second = client.post("/api/bom/variance", json=body2)
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first_id, "必须是同一条被覆盖，不是新增"

    rows = client.get(
        "/api/bom/variance", params={"shop_id": shop.id, "start": "2026-03-01", "end": "2026-03-31"}
    ).json()
    assert len(rows) == 1, f"一天只能有一条，实际 {len(rows)} 条"
    assert rows[0]["sold_qty"] == 120
    assert rows[0]["std_usage"] == pytest.approx(12.0)      # 120 × 0.1

    # 账面只被扣一次：期末账面 = 100 + 0 − 12 − 0 = 88
    detail = client.get(
        "/api/stock/variance",
        params={"shop_id": shop.id, "start": "2026-03-01", "end": "2026-03-31"},
    ).json()
    item = [i for i in detail["items"] if i["take_date"] == "2026-03-05"][0]
    assert item["book_reason"]["theoretical_usage_qty"] == pytest.approx(12.0)
    assert item["book_qty"] == pytest.approx(88.0), "重复提交不许让账面少扣两次"


def test_bom_variance_unique_index_exists_on_real_db():
    """审查项 G3 的另一半：老库（M5 第一轮建的表）也要补上唯一索引。

    `create_all` 只建新表，**不会给已存在的表加约束** ——
    不补的话，`data/checkup.db` 上这条唯一约束形同不存在。
    """
    from pathlib import Path
    from sqlalchemy import create_engine, inspect

    db_file = Path(__file__).resolve().parent.parent / "data" / "checkup.db"
    if not db_file.exists():
        pytest.skip("没有真库 data/checkup.db（CI 环境跳过）")
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        indexes = {ix["name"] for ix in inspect(engine).get_indexes("bom_variance")}
    finally:
        engine.dispose()
    assert "uq_bom_variance_day_dish_goods" in indexes, (
        "老库上缺唯一索引 —— 迁移没跑。重启后端会跑 migrate()，"
        "或手工跑 server.db.migrate(engine)"
    )


def test_book_qty_series_matches_pointwise(client, shop, session):
    """审查项 E5 的命门：区间递推版必须和逐张单算**逐分一致**。

    E5 为了把报告页的 N+1 查询降下来，加了"一次取数 + 递推"的批量版。
    递推和逐张算在数学上等价，但**等价性是断言出来的，不是我说出来的** ——
    一旦这里红了，说明两份算法已经漂移，必须回退批量版。
    """
    from server.stock_service import book_qty_series, compute_book_qty

    g = _goods(client, shop, name="复杂场景货", unit="斤", unit_price=3.0)
    # 灌一段有入库、有报损、有领用、有多次盘点的数据
    assert _stocktake(client, shop, g["id"], on="2026-03-01", actual_qty=100).status_code == 200
    assert client.post(
        "/api/bom",
        json={"shop_id": shop.id, "dish_name": "测试菜", "goods_id": g["id"],
              "std_qty_net": 0.5, "yield_rate": 1.0, "std_price": 3.0},
    ).status_code == 201
    for i, (day, qty) in enumerate((("2026-03-03", 20), ("2026-03-08", 15))):
        assert client.post(
            "/api/receipts",
            json={"shop_id": shop.id, "goods_id": g["id"], "purchase_date": day, "qty": qty},
        ).status_code == 201
        assert _stocktake(client, shop, g["id"], on=day, actual_qty=100 + i * 5 - 10,
                          reason_code="T08", approver="老板娘").status_code == 200
        assert client.post(
            "/api/bom/variance",
            json={"shop_id": shop.id, "biz_date": day, "dish_name": "测试菜",
                  "goods_id": g["id"], "sold_qty": 10},
        ).status_code == 201
    assert client.post(
        "/api/scraps",
        json={"shop_id": shop.id, "goods_id": g["id"], "scrap_date": "2026-03-09",
              "qty": 2, "reason_code": "T02"},
    ).status_code == 201

    series = book_qty_series(session, shop.id, g["id"],
                             date(2026, 3, 1), date(2026, 3, 31))
    assert series, "应该算出多次盘点的账面"
    for take_date, (batch_book, batch_why) in series.items():
        single_book, single_why = compute_book_qty(
            session, shop.id, g["id"], take_date
        )
        assert batch_book == pytest.approx(single_book), (
            f"{take_date}：递推 {batch_book} ≠ 逐张 {single_book}"
        )
        for key in ("base_qty", "inbound_qty", "theoretical_usage_qty", "scrap_qty", "has_base"):
            assert batch_why[key] == pytest.approx(single_why[key]), (
                f"{take_date}.{key}：递推 {batch_why[key]} ≠ 逐张 {single_why[key]}"
            )


def test_variance_report_query_count_drops(client, shop, session):
    """审查项 E5 的量化证据：差异报表的数据库往返次数必须显著下降。

    优化前：每张盘点单 4 次查询。150 张单 = 600 次往返。
    优化后：按货品分组，一次取数 + 递推。
    这条断言把"降了"变成数字，而不是"我觉得快了"。
    """
    from sqlalchemy import event

    from server.stock_service import variance_detail

    g = _goods(client, shop, name="性能验证货", unit="斤", unit_price=2.0)
    assert _stocktake(client, shop, g["id"], on="2026-03-01", actual_qty=100).status_code == 200
    # 造 20 次盘点（老板每月盘 20 次的量级）
    for i in range(20):
        day = date(2026, 3, 2) + timedelta(days=i)
        assert _stocktake(client, shop, g["id"], on=day.isoformat(), actual_qty=90,
                          reason_code="T08", approver="老板娘").status_code == 200

    counter = {"n": 0}

    @event.listens_for(session.connection().engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    try:
        variance_detail(session, shop.id, date(2026, 3, 1), date(2026, 3, 31))
    finally:
        pass
    # 21 张盘点单。优化前会到 84+ 次；优化后应该是个位数到十几次
    assert counter["n"] < 30, f"查询次数 {counter['n']} 太多了，N+1 没优化掉"


# =========================================================================== #
# M6 · 审批阈值「且」（docs/06-M6 §0 裁定 2 / §6 门禁 3）
# =========================================================================== #
def test_approval_threshold_requires_both_amount_and_rate(client, shop):
    """门禁 3 的**双向断言**：金额和比例都过线才要审批，缺一个都不要。

    `docs/06-M6` §6 原文：
    | 28 元/9%  → 不需审批 |
    | 500 元/6% → 需审批   |

    这是「且」语义的一体两面 —— 只断言"大的要拒"是不够的，
    必须同时断言"小的不拒"，否则实现悄悄退回"或"也发现不了。
    """
    from server.approvals import needs_approval

    # 28 元 / 9%：金额没过线 → 不要审批
    need, _why = needs_approval(28.0, 28.0 / 0.09)
    assert need is False, "28 元金额没过 100 元线，不该要签字"

    # 500 元 / 6%：金额过线 且 比例过线 → 要审批
    need, why = needs_approval(500.0, 500.0 / 0.06)
    assert need is True, "500 元 / 6% 两个条件都过了，必须审批"
    assert "金额" in why and "比例" in why

    # 补充两个方向，把边界钉死
    # 1000 元 / 3%：金额过线但比例不过 → 「且」语义下不要
    assert needs_approval(1000.0, 1000.0 / 0.03)[0] is False
    # 60 元 / 90%：比例过线但金额不过 → 「且」语义下不要
    assert needs_approval(60.0, 60.0 / 0.90)[0] is False


def test_approval_threshold_logic_regression_guard():
    """纯逻辑层的四象限表：把「且」关系穷举一遍，防止以后有人手滑改回「或」。"""
    from server.approvals import needs_approval

    cases = [
        # (金额, 比例基数, 期望是否需审批, 说明)
        (500.0, 500.0 / 0.06, True, "金额✓ 比例✓ → 要"),
        (500.0, 500.0 / 0.03, False, "金额✓ 比例✗ → 不要"),
        (28.0, 28.0 / 0.09, False, "金额✗ 比例✓ → 不要"),
        (28.0, 28.0 / 0.02, False, "金额✗ 比例✗ → 不要"),
        (100.0, 100.0 / 0.05, False, "金额=100（不>100）比例=5%（不>5%）→ 都不要"),
    ]
    for amount, base, expected, note in cases:
        got, _ = needs_approval(amount, base)
        assert got is expected, f"审批判定错：{note}，实际得到 {got}"


# =========================================================================== #
# M6 · 可解释损耗率（docs/06 §0 裁定 7 / §6 门禁 4）
# =========================================================================== #
def test_explained_loss_rate_splits_known_from_unknown(client, shop):
    """门禁 4：报告必须能说出「损耗 8000，其中 6500 有原因、1500 说不清」。

    这是 T01–T08 代码化 + M5-2 审批流**共同**换来的指标：
    只代码化不审批 → 自填自审，"已解释"是假的；
    只审批不代码化 → 有签字但没法按环节聚合。
    """
    _seed_period(client, shop.id, "2026-03-01", 3, revenue=5000, food=0.35)
    g = _goods(client, shop, name="海鲜", unit="斤", unit_price=100.0)
    buy = client.post(
        "/api/purchases",
        json={"shop_id": shop.id, "goods_id": g["id"], "purchase_date": "2026-03-02",
              "qty": 200, "unit_price": 100.0},
    )
    assert buy.status_code == 201, buy.text

    # 已登记 + 已审批的大额：6,500 元（65 斤 × 100）
    assert client.post(
        "/api/scraps",
        json={"shop_id": shop.id, "goods_id": g["id"], "scrap_date": "2026-03-10",
              "qty": 65, "reason_code": "T02", "approver": "老板娘"},
    ).status_code == 201
    # 已登记 + 已审批：1,500 元
    # ⚠️ M6 改「且」后这笔也必须有审批人：1500 元 ÷ 20000 元进货 = 7.5% > 5%，
    # 金额和比例都过线。无审批人会被 400 拒——这正是"且"阈值想达到的效果。
    assert client.post(
        "/api/scraps",
        json={"shop_id": shop.id, "goods_id": g["id"], "scrap_date": "2026-03-11",
              "qty": 15, "reason_code": "T05", "approver": "老板娘"},
    ).status_code == 201

    data = _report(client, shop)
    v = {m["code"]: m["value"] for m in data["metrics"]}
    assert v["scrap_amount"] == pytest.approx(8000.0)
    assert v["explained_loss_m"] == pytest.approx(8000.0)
    assert v["explained_loss_rate"] == pytest.approx(1.0)


def test_explained_loss_rate_counts_only_approved_as_explained(client, shop, session):
    """该签字没签的那部分**不算已解释** —— 否则自填自审，指标就是假的。

    行业口径「已登记**且通过审核**的损耗 ÷ 总损耗」，两个条件缺一不可。
    """
    _seed_period(client, shop.id, "2026-03-01", 3, revenue=5000, food=0.35)
    g = _goods(client, shop, name="牛肉", unit="斤", unit_price=100.0)
    buy = client.post(
        "/api/purchases",
        json={"shop_id": shop.id, "goods_id": g["id"], "purchase_date": "2026-03-02",
              "qty": 200, "unit_price": 100.0},
    )
    assert buy.status_code == 201, buy.text

    # 6,500 元：有原因 + 有审批人 → 已解释
    assert client.post(
        "/api/scraps",
        json={"shop_id": shop.id, "goods_id": g["id"], "scrap_date": "2026-03-10",
              "qty": 65, "reason_code": "T02", "approver": "老板娘"},
    ).status_code == 201
    # 1,500 元：有原因但**无审批人** → 说不清（自填自审不算数）
    #
    # ⚠️ **这条走不通 HTTP**：M6 改「且」阈值后，1500 元 ÷ 20000 元 = 7.5%，
    # 金额和比例都过线，无审批人会被 400 直接拒 —— 这正是阈值想达到的效果。
    # 但"无审批人的大额报废"在**老数据/直接写库**里仍然可能存在，
    # 所以这里用 ORM 直接造一条来验证口径（不绕过硬编码数据，也不假装 HTTP 能过）。
    session.add(
        Scrap(
            shop_id=shop.id, goods_id=g["id"], scrap_date=date(2026, 3, 11),
            qty=15, unit_price=100.0, amount=1500.0,
            reason_code="T05", approver=None,
        )
    )
    session.commit()

    data = _report(client, shop)
    v = {m["code"]: m["value"] for m in data["metrics"]}
    assert v["scrap_amount"] == pytest.approx(8000.0)
    assert v["explained_loss_m"] == pytest.approx(6500.0), "没签字的那 1500 不算已解释"
    assert v["explained_loss_rate"] == pytest.approx(6500.0 / 8000.0)


def test_explained_loss_rate_is_none_without_any_scrap(client, shop):
    """没有报损 → 返回 None，不返回 1.0（不给"100% 已解释"的假安心）。"""
    _seed_period(client, shop.id, "2026-03-01", 3, revenue=5000, food=0.35)
    data = _report(client, shop)
    v = {m["code"]: m["value"] for m in data["metrics"]}
    assert v["explained_loss_rate"] is None
    assert v["scrap_amount"] in (None, 0.0)


def test_explained_loss_rate_has_plain_text(client, shop):
    """指标必须有大白话模板，否则报告会退化成术语（守卫的同一条要求）。"""
    _seed_period(client, shop.id, "2026-03-01", 3, revenue=5000, food=0.35)
    g = _goods(client, shop, name="蔬菜", unit="斤", unit_price=5.0)
    assert client.post(
        "/api/scraps",
        json={"shop_id": shop.id, "goods_id": g["id"], "scrap_date": "2026-03-10",
              "qty": 4, "reason_code": "T03"},
    ).status_code == 201
    data = _report(client, shop)
    metric = next(m for m in data["metrics"] if m["code"] == "explained_loss_rate")
    assert metric["sentence"] and "说得清" in metric["sentence"]
