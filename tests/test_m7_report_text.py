"""M7 WP-2 · 报告文字缺陷测试（docs/08 §5.3，至少 6 条）。

D1 = 内部字段名泄露给老板（违反"对老板不出现术语"的 M0 硬规矩）
D2 = 空数据时第二行说"看着都在正常区间"（自相矛盾）+ 提示堆叠
"""

from __future__ import annotations

import re

from server.metrics_engine import MetricsEngine

JARGON = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")


# =========================================================================== #
# D1 · 内部字段名不泄露
# =========================================================================== #
def test_no_snake_case_leaks_in_any_missing_reason(client, shop, session):
    """用例 1：**遍历所有 enabled 指标、所有输入为 None**，missing 文字无 snake_case。

    这是最强的一条：不是只测某几个指标，而是把注册表里每个比率型指标
    都在"全空输入"下跑一遍 —— 将来加指标忘了更新翻译表，这条也会红。
    """
    engine = MetricsEngine(session)
    engine.reload()
    all_none = {code: None for code in engine.metric_defs}
    for fd in engine.field_defs.values():
        all_none[fd.code] = None

    reasons = engine.blocked_reasons(all_none)
    assert reasons, "全空输入下应该有缺项提示"
    for item in reasons:
        assert not JARGON.search(item["missing"]), (
            f"{item['code']} 的缺项文字泄露了内部名：{item['missing']!r}"
        )


def test_missing_reason_falls_back_to_registry_label(client, shop, session):
    """D1 兜底层：hints 没覆盖的名字用 metric_def.name / field_def.label。

    用 `revenue_m` 钉：它不在 hints 表里，但注册表里有中文名"月营业额"。
    断言出现在 missing 里的是中文名，不是裸码 —— 这就是兜底层在工作。
    """
    engine = MetricsEngine(session)
    engine.reload()
    assert "revenue_m" not in {
        "revenue", "purchase", "orders", "hours",
        "labor_m", "rent_m", "utility_m",
        "labor_cost", "labor_m_full", "revenue_m_avg",
        "stock_book_amount", "stock_variance_amount",
    }, "测试前提失效：revenue_m 应不在 hints 表里（否则测不到兜底层）"

    all_none = {code: None for code in engine.metric_defs}
    for fd in engine.field_defs.values():
        all_none[fd.code] = None

    text = "；".join(item["missing"] for item in engine.blocked_reasons(all_none))
    # 实测证据：`explained_loss_m` / `scrap_amount` / `gross_margin` 都不在 hints 表里，
    # 它们出现在 missing 里靠的是兜底层（metric_def.name "已解释损耗金额"/"报损金额"、
    # field_def.label "毛利率"）。断言这些中文名在场、裸码不在场。
    assert "已解释损耗金额" in text or "毛利率" in text, (
        f"兜底层好像没工作（既没有 metric_def.name 也没有 field_def.label 的翻译）：{text!r}"
    )
    assert not JARGON.search(text)


def test_missing_reason_deduplicates(client, shop, session):
    """D1 去重：两个内部名翻成同一句话时只出现一次（不出现"盘点、盘点"）。"""
    engine = MetricsEngine(session)
    engine.reload()
    all_none = {code: None for code in engine.metric_defs}
    for fd in engine.field_defs.values():
        all_none[fd.code] = None

    for item in engine.blocked_reasons(all_none):
        phrases = item["missing"].split("、")
        assert len(phrases) == len(set(phrases)), (
            f"{item['code']} 的缺项有重复：{item['missing']!r}"
        )


# =========================================================================== #
# D2 · 空数据自相矛盾 / 提示堆叠
# =========================================================================== #
def _report_json(client, shop):
    from datetime import date

    today = date.today().isoformat()
    return client.get(
        "/api/report", params={"shop_id": shop.id, "start": today, "end": today}
    ).json()


def test_empty_shop_warning_count_and_no_jargon(client, shop):
    """用例 2：空店报告 warnings ≤ 4，且无 snake_case。"""
    rep = _report_json(client, shop)
    warnings = rep["conclusion"]["warnings"]
    assert len(warnings) <= 4, f"提示堆了一屏：{warnings}"
    joined = "；".join(warnings)
    assert not JARGON.search(joined), f"warnings 里泄露内部字段名：{joined!r}"


def test_empty_shop_line2_does_not_claim_normal(client, shop):
    """用例 3：空店 lines[1] 不含"正常区间"——一个数都没填，没资格说这句。"""
    rep = _report_json(client, shop)
    lines = rep["conclusion"]["lines"]
    assert "正常区间" not in lines[1], f"空店还在说都正常：{lines[1]!r}"
    assert lines[1] == "有了营业额和买菜钱，才能判断高低。"


def test_normal_data_keeps_default_verdict(client, shop):
    """用例 4（防过度修复）：有数据且**所有优先指标都正常** → lines[1] 仍是 default_verdict。

    D2 只该改"没数据却说正常"的情况；真的比较过且都正常时，
    原来那句是对的，不能被误伤。
    数据设计：营业额 1180/天、买菜 390（33.1% ∈ 28–38）、排班 8h×25 元。
    → 食材率 ok；全口径人工率 ≈ 16.9% ∈ 15–20 ok；人效 147.5 ∈ 120–180 ok。
    （随便造个数不行：efficiency 偏高或人工率偏低都会抢走 lines[1]。）
    """
    from datetime import date, timedelta

    d0 = date(2026, 3, 1)
    for i in range(3):
        res = client.put(
            "/api/daily",
            json={
                "shop_id": shop.id,
                "stat_date": (d0 + timedelta(days=i)).isoformat(),
                "values": {"revenue": 1180, "purchase": 390},
                "shifts": [
                    {"worker_name": "小李", "start_time": "09:00", "end_time": "17:00"}
                ],
            },
        )
        assert res.status_code in (200, 201), res.text

    rep = client.get(
        "/api/report",
        params={"shop_id": shop.id, "start": "2026-03-01", "end": "2026-03-31"},
    ).json()
    lines = rep["conclusion"]["lines"]
    assert "正常区间" in lines[1], f"全指标正常时应该还是那句话：{lines[1]!r}"
    assert lines[1] != "这段时间还没有足够的数，先不判断高低。"


def test_high_food_cost_keeps_advice_high(client, shop):
    """用例 5：食材率偏高 → lines[1] 是 advice_high（行为不变）。"""
    from datetime import date, timedelta

    d0 = date(2026, 3, 1)
    for i in range(3):
        res = client.put(
            "/api/daily",
            json={
                "shop_id": shop.id,
                "stat_date": (d0 + timedelta(days=i)).isoformat(),
                "values": {"revenue": 4200, "purchase": 2200},   # 52% > 38% 上限
                "shifts": [
                    {"worker_name": "小李", "start_time": "09:00", "end_time": "17:00"}
                ],
            },
        )
        assert res.status_code in (200, 201), res.text

    rep = client.get(
        "/api/report",
        params={"shop_id": shop.id, "start": "2026-03-01", "end": "2026-03-31"},
    ).json()
    lines = rep["conclusion"]["lines"]
    assert "偏高" in lines[1], f"食材率 52% 应判偏高：{lines[1]!r}"


def test_blocked_still_has_everything_for_api(client, shop):
    """补充：截掉的缺项仍在返回体 blocked 字段里（API 调用方不受影响）。

    warnings 是给老板看的（≤4），blocked 是给程序用的（全量）——两本账分开。
    """
    rep = _report_json(client, shop)
    assert rep.get("blocked"), "空店的 blocked 不该是空的"
