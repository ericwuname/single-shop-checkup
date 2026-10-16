# -*- coding: utf-8 -*-
"""M4 测试：分类体系 / 主数据治理 / 入库 / 多店汇总 / 异常清单。

每条测试头部的「对应门禁」标注了它在 docs/04 §7 十门禁里承担哪一条。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from server import goods_master as gm


# =========================================================================== #
# 工具
# =========================================================================== #
def _goods(client, shop, *, name, spec=None, category_code="02-01", code=None, **extra) -> dict:
    body = {
        "shop_id": shop.id,
        "name": name,
        "spec": spec,
        "category_code": category_code,
        "confirmed_not_duplicate": True,
    }
    if code:
        body["code"] = code
    body.update(extra)
    res = client.post("/api/goods", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def _purchase(client, shop, goods_id, amount_qty, *, on="2026-03-01", unit_price=None) -> dict:
    body = {
        "shop_id": shop.id,
        "goods_id": goods_id,
        "purchase_date": on,
        "qty": amount_qty,
    }
    if unit_price is not None:
        body["unit_price"] = unit_price
    res = client.post("/api/purchases", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def _receipt(client, shop, goods_id, qty, *, on="2026-03-01", actual_net_qty=None) -> dict:
    body = {
        "shop_id": shop.id,
        "goods_id": goods_id,
        "purchase_date": on,
        "qty": qty,
    }
    if actual_net_qty is not None:
        body["actual_net_qty"] = actual_net_qty
    res = client.post("/api/receipts", json=body)
    return res


def _seed_revenue(client, shop, days=30, revenue=5000.0, start="2026-03-01") -> None:
    d0 = date.fromisoformat(start)
    for i in range(days):
        res = client.put(
            "/api/daily",
            json={
                "shop_id": shop.id,
                "stat_date": (d0 + timedelta(days=i)).isoformat(),
                "values": {"revenue": revenue, "orders": int(revenue / 30)},
            },
        )
        assert res.status_code == 200, res.text


# =========================================================================== #
# M4-3 · 分类体系
# =========================================================================== #
def test_category_tree_seeded_and_two_levels_only(client):
    """只拆两级：9 个一级 + 44 个二级，且**不许有三级**。"""
    level1 = client.get("/api/categories", params={"level": 1}).json()
    level2 = client.get("/api/categories", params={"level": 2}).json()

    assert len(level1) == 9
    assert len(level2) == 44
    assert {c["name"] for c in level1} == {
        "蔬菜类", "肉禽蛋类", "水产海鲜类", "粮油干货类", "调料类",
        "酒水饮料类", "包材类", "清洁类", "营销物料类",
    }
    for c in level2:
        assert c["level"] == 2
        assert c["parent_code"] in {x["code"] for x in level1}
        assert c["code"].count("-") == 1        # '02-01'：只有一层分隔，绝不允许三级
    # 一级之间不互相包含
    assert all(c["parent_code"] is None for c in level1)


def test_custom_second_level_can_be_added_and_selected(client):
    """老板能加二级，且加完立刻能在下拉里选到（预置覆盖 80%，剩下让他加）。"""
    res = client.post("/api/categories", json={"parent_code": "02", "name": "腌腊"})
    assert res.status_code == 201, res.text
    created = res.json()
    assert created["is_custom"] == 1
    assert created["code"].startswith("02-")

    codes = {c["code"] for c in client.get("/api/categories", params={"level": 2}).json()}
    assert created["code"] in codes


def test_goods_must_hang_on_second_level(client, shop):
    """商品必须挂**二级**分类；挂一级要被拒（否则成本结构表会串层）。"""
    res = client.post(
        "/api/goods",
        json={"shop_id": shop.id, "name": "随便什么", "category_code": "02",
              "confirmed_not_duplicate": True},
    )
    assert res.status_code == 400
    assert "二级" in res.text


def test_category_cost_sorted_and_main_driver_named(client, shop):
    """门禁 4：分类成本结构按金额降序 + 能说出"哪一类是主因"。"""
    meat = _goods(client, shop, name="五花肉", spec="散装", category_code="02-01",
                  unit="斤", unit_price=16.5)
    veg = _goods(client, shop, name="小油菜", spec="散装", category_code="01-01",
                 unit="斤", unit_price=3.2)
    oil = _goods(client, shop, name="大豆油", spec="5L/桶", category_code="04-02",
                 unit="L", purchase_unit="桶", unit_conversion=5.0, unit_price=9.4)

    # 上期（2025-09）：肉 30% / 菜 50% / 油 20%
    for gid, amount, on in ((meat["id"], 300, "2026-02-10"),
                            (veg["id"], 500, "2026-02-10"),
                            (oil["id"], 200, "2026-02-10")):
        _purchase(client, shop, gid, 1, on=on, unit_price=amount)
    # 本期（2026-03）：肉 45% / 菜 35% / 油 20%  ← **肉涨幅最大**，所以它是主因
    for gid, amount, on in ((meat["id"], 4500, "2026-03-05"),
                            (veg["id"], 3500, "2026-03-05"),
                            (oil["id"], 2000, "2026-03-05")):
        _purchase(client, shop, gid, 1, on=on, unit_price=amount)

    data = client.get(
        "/api/report/category-cost",
        params={"shop_id": shop.id, "start": "2026-03-01", "end": "2026-03-31"},
    ).json()

    assert data["total"] == pytest.approx(10000.0)
    amounts = [i["amount"] for i in data["items"]]
    assert amounts == sorted(amounts, reverse=True), "必须按金额降序"

    meat_item = next(i for i in data["items"] if i["code"] == "02")
    assert meat_item["share"] == pytest.approx(0.45, abs=0.001)
    assert meat_item["previous_share"] == pytest.approx(0.30, abs=0.001)
    assert meat_item["direction"] == "升"
    assert meat_item["is_main_driver"] is True
    assert "主因" in data["conclusion"]
    assert "肉禽蛋类" in data["conclusion"]


def test_category_cost_share_is_sum_over_sum_not_mean_of_days(client, shop):
    """占比必须 Σ分子 ÷ Σ分母 重算，不是"每天占比取平均"。

    D1：肉 90 / 总 100（90%）   D2：肉 10 / 总 900（1.1%）
    正确（Σ/Σ）= 100/1000 = 10%    错误（日均）= (90% + 1.1%)/2 ≈ 45.6%
    """
    meat = _goods(client, shop, name="五花肉", spec="散装", category_code="02-01")
    veg = _goods(client, shop, name="小油菜", spec="散装", category_code="01-01")

    _purchase(client, shop, meat["id"], 1, on="2026-03-01", unit_price=90)
    _purchase(client, shop, veg["id"], 1, on="2026-03-01", unit_price=10)
    _purchase(client, shop, meat["id"], 1, on="2026-03-02", unit_price=10)
    _purchase(client, shop, veg["id"], 1, on="2026-03-02", unit_price=890)

    data = client.get(
        "/api/report/category-cost",
        params={"shop_id": shop.id, "start": "2026-03-01", "end": "2026-03-02"},
    ).json()
    meat_item = next(i for i in data["items"] if i["code"] == "02")
    assert meat_item["share"] == pytest.approx(0.10, abs=0.001)
    assert abs(meat_item["share"] - 0.456) > 0.1, "绝不能是每天占比取平均"


# =========================================================================== #
# M4-10 / M4-11 · 规格与三单位换算
# =========================================================================== #
def test_same_name_different_spec_is_not_duplicate(client, shop):
    """「大豆油 5L」vs「大豆油 20L」→ 提示但**不算重复**（规格不同就是两个 SKU）。"""
    _goods(client, shop, name="大豆油", spec="20L/桶", category_code="04-02",
           unit="L", purchase_unit="桶", unit_conversion=20.0)

    res = client.post(
        "/api/goods/check-duplicates",
        params={"shop_id": shop.id, "name": "大豆油", "spec": "5L/桶"},
    ).json()
    assert res["candidates"], "应该提示有相似物料"
    assert res["candidates"][0]["name"] == "大豆油"
    assert res["must_confirm"] is False, "规格不同不该强制确认"
    assert "不同规格" in res["candidates"][0]["reason"]

    # 真的能建出来
    created = _goods(client, shop, name="大豆油", spec="5L/桶", category_code="04-02",
                     unit="L", purchase_unit="桶", unit_conversion=5.0)
    assert created["spec"] == "5L/桶"
    assert created["code"] != _goods.__name__  # 只是确认没抛异常


def test_same_name_same_spec_must_confirm(client, shop):
    """「可乐 330ml」vs「可口可乐 330ml」+「可乐 330ml」→ 高度相似必须人工确认。"""
    _goods(client, shop, name="可乐", spec="330ml/罐", category_code="06-02", unit="罐")

    res = client.post(
        "/api/goods/check-duplicates",
        params={"shop_id": shop.id, "name": "可乐", "spec": "330ml/罐"},
    ).json()
    assert res["must_confirm"] is True

    # 不确认 → 409
    blocked = client.post(
        "/api/goods",
        json={"shop_id": shop.id, "name": "可乐", "spec": "330ml/罐",
              "category_code": "06-02", "unit": "罐"},
    )
    assert blocked.status_code == 409
    assert "同名同规格" in blocked.text

    # 确认后能建
    ok = client.post(
        "/api/goods",
        json={"shop_id": shop.id, "name": "可乐", "spec": "330ml/罐",
              "category_code": "06-02", "unit": "罐", "confirmed_not_duplicate": True},
    )
    assert ok.status_code == 201

    # 异名同物（可口可乐）也建议确认
    other = client.post(
        "/api/goods/check-duplicates",
        params={"shop_id": shop.id, "name": "可口可乐", "spec": "330ml/罐"},
    ).json()
    assert other["candidates"], "「可口可乐」应该提示与「可乐」相似"


def test_code_rule_and_spec_suffix(client, shop):
    """编码规则：[大类两位]-[中类两位]-[序号三位]-[规格两位]，且编码里不写供应商/门店。"""
    a = _goods(client, shop, name="大豆油", spec="5L/桶", spec_code="01", category_code="04-02")
    b = _goods(client, shop, name="大豆油", spec="20L/桶", spec_code="02", category_code="04-02")
    assert gm.CODE_RE.match(a["code"]), a["code"]
    assert gm.CODE_RE.match(b["code"]), b["code"]
    assert a["code"].startswith("04-02-")
    assert a["code"] != b["code"]
    assert "店" not in a["code"] and len(a["code"]) <= 12


def test_three_units_and_conversion(client, shop):
    """门禁 9：1 箱 = 20 L，采购 5 箱 → 入库 100 L。"""
    oil = _goods(client, shop, name="大豆油", spec="20L/桶", category_code="04-02",
                 unit="L", purchase_unit="箱", usage_unit="勺",
                 unit_conversion=20.0, stock_to_usage=50.0, unit_price=8.9)

    assert oil["stock_unit"] == "L"              # 库存单位（基准）
    assert oil["purchase_to_stock"] == 20.0      # 换算率的对外别名
    assert oil["usage_unit"] == "勺"

    res = client.post(
        "/api/receipts",
        json={"shop_id": shop.id, "goods_id": oil["id"], "purchase_date": "2026-03-01", "qty": 5},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["unit"] == "箱"
    assert body["qty_in_stock_unit"] == pytest.approx(100.0)     # 5 箱 × 20 L
    assert body["amount"] == pytest.approx(5 * 8.9 * 20)


def test_weighed_goods_requires_actual_weight(client, shop):
    """门禁 9 后半：称重品不填实际净重 → 拒绝提交（包装上的固定重量不是实重）。"""
    beef = _goods(client, shop, name="鲜牛肉", spec="散装", category_code="02-02",
                  unit="斤", purchase_unit="斤", is_weighed=1)

    bad = client.post(
        "/api/receipts",
        json={"shop_id": shop.id, "goods_id": beef["id"], "purchase_date": "2026-03-01", "qty": 10},
    )
    assert bad.status_code == 400
    assert "实际净重" in bad.text

    good = client.post(
        "/api/receipts",
        json={"shop_id": shop.id, "goods_id": beef["id"], "purchase_date": "2026-03-01",
              "qty": 10, "actual_net_qty": 9.3},
    )
    assert good.status_code == 201, good.text
    # 以实重入库，不是 10
    assert good.json()["qty_in_stock_unit"] == pytest.approx(9.3)


def test_unit_conversion_change_is_traced(client, shop):
    """换算系数变更必须留痕（供应商改包装是高频事件，改了不记就对不上账）。"""
    oil = _goods(client, shop, name="大豆油", spec="5L/桶", category_code="04-02",
                 purchase_unit="桶", unit_conversion=5.0)
    res = client.patch(
        f"/api/goods/{oil['id']}",
        json={"unit_conversion": 6.0, "unit_conversion_change_note": "供应商换了包装"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["unit_conversion"] == pytest.approx(6.0)
    assert body["unit_conversion_changed_at"] is not None
    assert "供应商换了包装" in (body["unit_conversion_change_note"] or "")


# =========================================================================== #
# M4-12 · 合并 / 停用 / 红线
# =========================================================================== #
def test_code_cannot_change_once_referenced(client, shop):
    """🔴 红线：编码被单据引用过 → 拒绝修改，只能停用后新建。"""
    oil = _goods(client, shop, name="大豆油", spec="5L/桶", category_code="04-02", code="04-02-001-01")
    # 先改一次没事（还没被引用）
    ok = client.patch(f"/api/goods/{oil['id']}", params={"code": "04-02-009-01"}, json={})
    assert ok.status_code == 200, ok.text

    _purchase(client, shop, oil["id"], 1, unit_price=47)

    blocked = client.patch(f"/api/goods/{oil['id']}", params={"code": "04-02-010-01"}, json={})
    assert blocked.status_code == 409
    assert "不可修改" in blocked.text

    # 停用是允许的（停用 ≠ 删除：历史单据还指着它）
    off = client.patch(f"/api/goods/{oil['id']}", json={"enabled": 0})
    assert off.status_code == 200
    assert off.json()["enabled"] == 0


def test_merge_collects_history_and_retires_duplicate(client, shop):
    """门禁 8：合并两个重复码 → 副码停用 + 指向主码 + 历史数据归集到主码。"""
    main = _goods(client, shop, name="番茄", spec="散装", category_code="01-03", code="01-03-001-01")
    dup = _goods(client, shop, name="西红柿", spec="散装", category_code="01-03", code="01-03-002-01")

    _purchase(client, shop, dup["id"], 1, unit_price=120, on="2026-03-03")

    res = client.post(
        "/api/goods/merge",
        json={"source_ids": [dup["id"]], "target_id": main["id"]},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["merged"] == [dup["code"]]
    assert body["moved_records"] >= 1

    # 副码：停用 + 指向主码（不删除）
    after = client.get(f"/api/goods/{dup['id']}").json()
    assert after["enabled"] == 0
    assert after["merged_into_id"] == main["id"]

    # 历史单据归集到主码 → 分类成本结构里不再分裂
    rows = client.get(
        "/api/purchases", params={"shop_id": shop.id, "start": "2026-03-01", "end": "2026-03-31"}
    ).json()
    assert {r["goods_id"] for r in rows} == {main["id"]}


def test_alias_and_pinyin_search(client, shop):
    """门禁 8：番茄/西红柿 通过别名合并统计；拼音首字母能检索（"kl" → 可乐）。"""
    tomato = _goods(client, shop, name="番茄", spec="散装", category_code="01-03")
    cola = _goods(client, shop, name="可乐", spec="330ml/罐", category_code="06-02")

    res = client.post(f"/api/goods/{tomato['id']}/aliases", json={"alias": "西红柿"})
    assert res.status_code == 201, res.text

    by_alias = client.get("/api/goods", params={"shop_id": shop.id, "q": "西红柿"}).json()
    assert [g["id"] for g in by_alias] == [tomato["id"]]

    by_pinyin = client.get("/api/goods", params={"shop_id": shop.id, "q": "kl"}).json()
    assert cola["id"] in [g["id"] for g in by_pinyin]

    by_spec = client.get("/api/goods", params={"shop_id": shop.id, "q": "330ml"}).json()
    assert cola["id"] in [g["id"] for g in by_spec]


def test_search_ignores_disabled_goods(client, shop):
    g = _goods(client, shop, name="下架料", spec="散装", category_code="05-01")
    client.patch(f"/api/goods/{g['id']}", json={"enabled": 0})
    hits = client.get("/api/goods", params={"shop_id": shop.id, "q": "下架"}).json()
    assert hits == []


# =========================================================================== #
# M4-4 · 账面库存自动推算
# =========================================================================== #
def test_book_qty_computed_from_receipts_and_scraps(client, shop):
    """门禁 5：账面库存不再靠手填 = 上期实盘 + Σ入库 − Σ报损。"""
    oil = _goods(client, shop, name="大豆油", spec="5L/桶", category_code="04-02",
                 unit="L", purchase_unit="桶", unit_conversion=5.0, unit_price=9.4)

    # 期初实盘 10 L
    first = client.put(
        "/api/stocktakes",
        json={"shop_id": shop.id, "goods_id": oil["id"], "take_date": "2026-03-01",
              "actual_qty": 10},
    )
    assert first.status_code == 200, first.text
    assert first.json()["book_qty"] is None, "M4-4 之后账面由系统推算，不必再手填"

    # 期中入库 5 桶 = 25 L
    _receipt(client, shop, oil["id"], 5, on="2026-03-05")
    # 期中报损 2 L（M5-2 起原因必须代码化）
    scrap = client.post(
        "/api/scraps",
        json={"shop_id": shop.id, "goods_id": oil["id"], "scrap_date": "2026-03-06",
              "qty": 2, "unit_price": 9.4, "reason_code": "T02"},
    )
    assert scrap.status_code == 201, scrap.text

    # 期末再盘一次：这次系统就能算出账面 = 期初实盘 10 + 入库 25 − 报损 2 = 33。
    # 差 3 L = 28.2 元，金额没过 100 元阈值，但**比例 9% > 5%** → 按任务书 §5 要审批人
    last = client.put(
        "/api/stocktakes",
        json={"shop_id": shop.id, "goods_id": oil["id"], "take_date": "2026-03-31",
              "actual_qty": 30, "reason_code": "T08", "approver": "老板娘"},
    )
    assert last.status_code == 200, last.text

    detail = client.get(
        "/api/stock/variance",
        params={"shop_id": shop.id, "start": "2026-03-01", "end": "2026-03-31"},
    ).json()
    # 条目按日期排，期末那条才是"用入库推出来的"
    item = detail["items"][-1]
    assert item["take_date"] == "2026-03-31"
    assert item["book_qty"] == pytest.approx(10 + 25 - 2), item
    assert item["reported_book_qty"] is None
    assert item["book_qty_source"] == "系统推算"
    assert item["book_reason"]["base_from"] == "2026-03-01 实盘"
    assert item["book_reason"]["inbound_qty"] == pytest.approx(25.0)
    assert item["book_reason"]["scrap_qty"] == pytest.approx(2.0)
    # ⚠️ M4 时这条写的是"missing 里必须标注仍缺领用（bom 未解锁）"。
    # docs/05 §3 解锁 bom 后领用已进公式 —— **没有缺项了**，所以改成断言
    # "公式里确实有理论领用这一项且本次为 0"（不是删断言）。
    assert item["book_reason"]["formula"] == "上期实盘 + Σ入库 − Σ理论领用 − Σ报损"
    assert item["book_reason"]["theoretical_usage_qty"] == pytest.approx(0.0)
    assert item["book_reason"]["missing"] is None, "M5-0 之后账面公式不再缺项"
    assert item["variance_qty"] == pytest.approx(30 - 33)
    assert "账面应该剩 33" in detail["sentence"]


def test_book_qty_falls_back_to_reported_when_no_basis(client, shop):
    """系统完全推不出来时（无历史、无入库）→ 退回老板自报，而不是硬说账面是 0。"""
    g = _goods(client, shop, name="孤品", spec="散装", category_code="05-01")
    client.put(
        "/api/stocktakes",
        json={"shop_id": shop.id, "goods_id": g["id"], "take_date": "2026-03-01",
              "book_qty": 50, "actual_qty": 47},
    )
    detail = client.get(
        "/api/stock/variance",
        params={"shop_id": shop.id, "start": "2026-03-01", "end": "2026-03-31"},
    ).json()
    item = detail["items"][0]
    assert item["book_qty"] == pytest.approx(50.0)
    assert item["variance_qty"] == pytest.approx(-3.0)


# =========================================================================== #
# M4-6 / M4-7 · 多店汇总与对比
# =========================================================================== #
def _make_shop(client, name, owner_key, revenue, labor):
    shop = client.post(
        "/api/shops",
        json={"name": name, "category": "catering", "default_hourly_rate": 25},
    ).json()
    client.patch(f"/api/shops/{shop['id']}", json={"owner_key": owner_key})
    client.put(
        f"/api/shops/{shop['id']}/monthly",
        json={"month": "2026-03", "values": {"rent_m": 1000, "utility_m": 200,
                                            "gross_margin": 0.6}},
    )

    # 营业额：一天一天记太慢，用 30 天摊（保证日均口径一致）
    d0 = date(2026, 3, 1)
    for i in range(30):
        client.put(
            "/api/daily",
            json={
                "shop_id": shop["id"],
                "stat_date": (d0 + timedelta(days=i)).isoformat(),
                "values": {"revenue": revenue / 30},
            },
        )
    # 人工：30 天每天都排 8 小时，时薪调到让 labor **总额**正确
    rate = labor / 30 / 8
    for i in range(30):
        client.put(
            "/api/daily",
            json={
                "shop_id": shop["id"],
                "stat_date": (d0 + timedelta(days=i)).isoformat(),
                "values": {"revenue": revenue / 30},
                "shifts": [{"worker_name": "员工A", "start_time": "09:00", "end_time": "17:00",
                            "hourly_rate": rate}],
            },
        )
    return shop


def test_portfolio_rate_is_sum_over_sum_not_mean(client):
    """门禁 6：三家店 100/300/500、人工 25/60/100 → 汇总 = 185/900 = **20.56%**。

    并**双向断言**远离"对各店比率取平均"的 21.67%。
    """
    owner = "boss-A"
    shops = [
        _make_shop(client, f"{owner}-1", owner, 100, 25),
        _make_shop(client, f"{owner}-2", owner, 300, 60),
        _make_shop(client, f"{owner}-3", owner, 500, 100),
    ]

    data = client.get(
        "/api/portfolio",
        params={"owner_key": owner, "start": "2026-03-01", "end": "2026-03-30"},
    ).json()

    assert data["shop_count"] == 3
    assert data["metrics"]["revenue"] == pytest.approx(900.0)
    assert data["metrics"]["labor_cost"] == pytest.approx(185.0)

    porter = data["metrics"]["labor_cost_rate_full"]
    assert porter == pytest.approx(185 / 900, abs=1e-4)
    # 明确排除"三店比率取平均"那条错路
    assert abs(porter - 21.67 / 100) > 0.005

    # 对比表 + 排名
    rows = {r["shop_id"]: r for r in data["shops"]}
    assert len(rows) == 3
    assert all(r["rank"].get("revenue") for r in data["shops"])
    top = min(data["shops"], key=lambda r: r["rank"]["revenue"])
    assert top["shop_name"] == f"{owner}-3"          # 营业额 500 那家排第一
    assert data["best_shop"] and data["worst_shop"]
    assert data["best_shop"]["shop_name"] != data["worst_shop"]["shop_name"]


def test_portfolio_excludes_inactive_and_other_owners(client):
    owner = "boss-B"
    a = _make_shop(client, "B-1", owner, 300, 60)
    _make_shop(client, "B-2", owner, 300, 60)
    _make_shop(client, "C-1", "boss-C", 999, 1)      # 别人家的店

    data = client.get(
        "/api/portfolio",
        params={"owner_key": owner, "start": "2026-03-01", "end": "2026-03-30"},
    ).json()
    assert data["shop_count"] == 2
    assert 999 not in [r["revenue"] for r in data["shops"]]

    # 关店后不进汇总，但历史还在
    client.patch(f"/api/shops/{a['id']}", json={"is_active": 0})
    data2 = client.get(
        "/api/portfolio",
        params={"owner_key": owner, "start": "2026-03-01", "end": "2026-03-30"},
    ).json()
    assert data2["shop_count"] == 1
    assert client.get(f"/api/shops/{a['id']}").json()["is_active"] == 0


def test_shop_list_filters_by_owner(client):
    _make_shop(client, "D-1", "boss-D", 100, 20)
    _make_shop(client, "E-1", "boss-E", 100, 20)
    only_d = client.get("/api/shops", params={"owner_key": "boss-D"}).json()
    assert [s["name"] for s in only_d] == ["D-1"]
    active = client.get("/api/shops", params={"owner_key": "boss-D", "active_only": True}).json()
    assert len(active) == 1


# =========================================================================== #
# M4-9 · 异常清单
# =========================================================================== #
def test_anomalies_sorted_by_amount_desc(client, shop):
    """门禁 7：异常清单按金额降序 —— 老板先看钱最多的那个。"""
    _seed_revenue(client, shop, days=30, revenue=10000)
    # 食材率 60%（远超 38%）→ 折算成钱应该很大
    for i in range(30):
        client.put(
            "/api/daily",
            json={
                "shop_id": shop.id,
                "stat_date": (date(2026, 3, 1) + timedelta(days=i)).isoformat(),
                "values": {"revenue": 10000, "purchase": 6000, "orders": 300},
            },
        )
    data = client.get(
        "/api/report", params={"shop_id": shop.id, "start": "2026-03-01", "end": "2026-03-30"}
    ).json()

    anomalies = data["anomalies"]
    assert anomalies, "食材率 60% 必须出现在异常清单里"
    amounts = [a["amount"] for a in anomalies]
    assert amounts == sorted(amounts, reverse=True), "必须按金额降序"
    assert anomalies[0]["code"] == "food_cost_rate"
    assert anomalies[0]["amount"] > 0
    assert anomalies[0]["sentence"]


def test_monthly_series_has_twelve_points(client, shop):
    _seed_revenue(client, shop, days=30, revenue=3000)
    # 保本线要人工才能算：没有排班时 labor_m 是 None（不是 0），保本线跟着 None
    client.put(
        "/api/daily",
        json={"shop_id": shop.id, "stat_date": "2026-03-01", "values": {"revenue": 3000},
              "shifts": [{"worker_name": "员工A", "start_time": "09:00", "end_time": "17:00"}]},
    )
    client.put(
        f"/api/shops/{shop.id}/monthly",
        json={"month": "2026-03", "values": {"rent_m": 6000, "utility_m": 1500,
                                            "gross_margin": 0.6}},
    )
    data = client.get(
        "/api/report/monthly-series",
        params={"shop_id": shop.id, "months": 12, "anchor": "2026-03-15"},
    ).json()
    pts = data["points"]
    assert len(pts) == 12
    assert pts[-1]["month"] == "2026-03"
    assert pts[0]["month"] == "2025-04"
    assert pts[-1]["revenue"] == pytest.approx(90000.0)
    assert pts[-1]["breakeven_daily"] is not None
    assert pts[0]["revenue"] is None      # 没数据的月份给 None，不是 0
