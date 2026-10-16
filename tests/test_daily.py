"""录入与报告的行为测试（施工单 3.6 边界情况 + 验收硬门禁 #4）。

包含一条**话术护栏**（test_conclusion_has_no_jargon）：
施工单 1.4 明确「绝不出现财务术语」，只靠人记是记不住的，交给单测守。
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import text

from conftest import PERIOD_END, PERIOD_START
from server.metrics_engine import shift_hours
from server.models import FactDaily, MetricValue, Shift
from server.routers.report import assemble_report

#: 施工单 1.4 点名的词 + 同类财务术语，一律不许出现在给老板看的话里
FORBIDDEN_TERMS = (
    "变动成本",
    "边际贡献",
    "边际成本",
    "保本点",
    "盈亏平衡",
    "固定成本",
    "毛利",
    "净利",
    "利润",
    "ROI",
    "投资回报",
    "现金流",
    "资产负债",
)

#: fixtures/sample.json 的手工 Excel 口径结果（施工单门禁 4：误差 < 0.01）
EXPECTED = {
    "revenue": 23000.0,
    "purchase": 5750.0,
    "orders": 755.0,
    "hours": 38.0,
    "labor_cost": 950.0,
    "labor_cost_rate": 950 / 23000,
    "food_cost_rate": 5750 / 23000,
    "avg_ticket": 23000 / 755,
    "efficiency": 23000 / 38,
    "breakeven_daily": (6000.0 + 1500.0 + 950.0) / 30 / 0.6,
}


@pytest.fixture()
def sample_json() -> dict:
    from conftest import FIXTURES

    return json.loads((FIXTURES / "sample.json").read_text(encoding="utf-8"))


def _push_sample_over_http(client, shop_id: int, data: dict) -> None:
    """把 sample.json 通过 HTTP 灌进服务 —— 走真实录入链路，不走 ORM。"""
    res = client.put(
        f"/api/shops/{shop_id}/monthly",
        json={"month": data["monthly"]["month"], "values": data["monthly"]["values"]},
    )
    assert res.status_code == 200, res.text

    for day in data["daily"]:
        res = client.put(
            "/api/daily",
            json={
                "shop_id": shop_id,
                "stat_date": day["stat_date"],
                "values": day["values"],
                "shifts": day["shifts"],
            },
        )
        assert res.status_code == 200, res.text


def _report(client, shop_id: int) -> dict:
    res = client.get(
        "/api/report",
        params={"shop_id": shop_id, "start": PERIOD_START.isoformat(), "end": PERIOD_END.isoformat()},
    )
    assert res.status_code == 200, res.text
    return res.json()


# =========================================================================== #
# 1. 边界情况（施工单 3.6）
# =========================================================================== #
def test_overnight_shift_counts_eight_hours(client, shop):
    res = client.put(
        "/api/daily",
        json={
            "shop_id": shop.id,
            "stat_date": "2026-10-03",
            "values": {"revenue": 4000},
            "shifts": [
                {"worker_name": "员工C", "start_time": "22:00", "end_time": "06:00"},
            ],
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["shifts"][0]["hours"] == pytest.approx(8.0)

    res = client.get("/api/report", params={"shop_id": shop.id, "start": "2026-10-03", "end": "2026-10-03"})
    m = {x["code"]: x["value"] for x in res.json()["metrics"]}
    assert m["hours"] == pytest.approx(8.0)
    assert m["labor_cost"] == pytest.approx(8 * 25.0)


def test_zero_length_shift_is_skipped_silently(client, shop):
    """工时为空 → 不写入，不报错。"""
    res = client.put(
        "/api/daily",
        json={
            "shop_id": shop.id,
            "stat_date": "2026-10-04",
            "values": {"revenue": 1000},
            "shifts": [{"worker_name": "员工A", "start_time": "18:00", "end_time": "18:00"}],
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["shifts"] == []


def test_no_shifts_at_all_is_fine(client, shop):
    res = client.put(
        "/api/daily",
        json={"shop_id": shop.id, "stat_date": "2026-10-05", "values": {"revenue": 1500}, "shifts": []},
    )
    assert res.status_code == 200, res.text
    m = {x["code"]: x["value"] for x in _report(client, shop.id)["metrics"]}
    assert m["hours"] is None
    assert m["efficiency"] is None           # 缺失不是 0，也不抛异常


def test_duplicate_save_overwrites_not_appends(client, shop):
    payload = {"shop_id": shop.id, "stat_date": "2026-10-06", "values": {"revenue": 1000}}
    assert client.put("/api/daily", json=payload).status_code == 200
    payload["values"] = {"revenue": 2222}
    assert client.put("/api/daily", json=payload).status_code == 200

    rows = client.get(
        "/api/daily", params={"shop_id": shop.id, "start": "2026-10-06", "end": "2026-10-06"}
    ).json()
    assert len(rows) == 1
    assert rows[0]["values"]["revenue"] == pytest.approx(2222)


def test_zero_revenue_does_not_divide_by_zero(client, shop):
    res = client.put(
        "/api/daily",
        json={
            "shop_id": shop.id,
            "stat_date": "2026-10-07",
            "values": {"revenue": 0, "purchase": 300, "orders": 0},
            "shifts": [{"worker_name": "员工A", "start_time": "18:00", "end_time": "22:00"}],
        },
    )
    assert res.status_code == 200, res.text
    m = {x["code"]: x["value"] for x in _report(client, shop.id)["metrics"]}
    assert m["revenue"] == pytest.approx(0.0)
    assert m["labor_cost_rate"] is None
    assert m["avg_ticket"] is None
    assert m["food_cost_rate"] is None


def test_invalid_time_format_rejected(client, shop):
    res = client.put(
        "/api/daily",
        json={
            "shop_id": shop.id,
            "stat_date": "2026-10-08",
            "values": {"revenue": 100},
            "shifts": [{"worker_name": "员工A", "start_time": "6pm", "end_time": "22:00"}],
        },
    )
    assert res.status_code == 422


def test_unknown_field_code_rejected(client, shop):
    res = client.put(
        "/api/daily",
        json={"shop_id": shop.id, "stat_date": "2026-10-09", "values": {"wild_field": 1}},
    )
    assert res.status_code == 400
    assert "field_def" in res.json()["detail"]


def test_scope_mismatch_rejected(client, shop):
    """月口径字段不能写进日口径的行 —— 口径串了会静默把数算错。"""
    res = client.put(
        "/api/daily",
        json={"shop_id": shop.id, "stat_date": "2026-10-10", "values": {"rent_m": 6000}},
    )
    assert res.status_code == 400
    assert "口径" in res.json()["detail"]


def test_empty_submission_rejected(client, shop):
    res = client.put("/api/daily", json={"shop_id": shop.id, "stat_date": "2026-10-11"})
    assert res.status_code == 400


def test_monthly_roundtrip(client, shop):
    res = client.put(
        f"/api/shops/{shop.id}/monthly",
        json={"month": "2026-10", "values": {"rent_m": 6000, "utility_m": 1500, "gross_margin": 0.6}},
    )
    assert res.status_code == 200, res.text
    assert res.json()["values"]["rent_m"] == pytest.approx(6000)

    res = client.get(f"/api/shops/{shop.id}/monthly", params={"month": "2026-10"})
    assert res.status_code == 200
    assert res.json()["values"]["utility_m"] == pytest.approx(1500)

    # 月格式非法由 Pydantic 在校验层拦下（422），根本进不到路由
    bad = client.put(f"/api/shops/{shop.id}/monthly", json={"month": "2026/10", "values": {}})
    assert bad.status_code == 422

    # 日口径字段不能写进月行
    wrong_scope = client.put(
        f"/api/shops/{shop.id}/monthly", json={"month": "2026-10", "values": {"revenue": 100}}
    )
    assert wrong_scope.status_code == 400
    assert "口径" in wrong_scope.json()["detail"]


def test_delete_day_removes_facts_and_shifts(client, session, shop):
    client.put(
        "/api/daily",
        json={
            "shop_id": shop.id,
            "stat_date": "2026-10-12",
            "values": {"revenue": 800},
            "shifts": [{"worker_name": "员工A", "start_time": "18:00", "end_time": "22:00"}],
        },
    )
    assert client.delete(f"/api/daily/{shop.id}/2026-10-12").status_code == 204
    assert session.query(FactDaily).filter_by(shop_id=shop.id).count() == 0
    assert session.query(Shift).filter_by(shop_id=shop.id).count() == 0


# =========================================================================== #
# 2. ⭐ 验收硬门禁 #4：5 个指标与手工 Excel 一致（误差 < 0.01）
# =========================================================================== #
def test_gate4_end_to_end_http_matches_manual_excel(client, sample_json, shop):
    """全链路：HTTP 录入 → HTTP 出报告 → 与手工算的数逐个对。

    数据来自 fixtures/sample.json，期望值见该文件的 _手工核对结果_Excel口径。
    """
    _push_sample_over_http(client, shop.id, sample_json)
    data = _report(client, shop.id)
    values = {m["code"]: m["value"] for m in data["metrics"]}

    for code, expected in EXPECTED.items():
        assert values[code] == pytest.approx(expected, abs=0.01), f"{code} 与手工计算不一致"

    # 落库缓存也要在（metric_value 是给后续 M2 做趋势的地基）
    assert data["period"]["days_with_data"] == 7


def test_gate4_metric_value_persisted(session, sample_shop):
    assemble_report(session, sample_shop.id, PERIOD_START, PERIOD_END)
    session.commit()
    cached = {
        row.metric_code: row.value
        for row in session.query(MetricValue).filter_by(shop_id=sample_shop.id).all()
    }
    assert cached["revenue"] == pytest.approx(EXPECTED["revenue"], abs=0.01)
    assert cached["breakeven_daily"] == pytest.approx(EXPECTED["breakeven_daily"], abs=0.01)


# =========================================================================== #
# 3. 报告与三行结论
# =========================================================================== #
def test_report_has_exactly_three_conclusion_lines(client, sample_json, shop):
    _push_sample_over_http(client, shop.id, sample_json)
    data = _report(client, shop.id)
    lines = data["conclusion"]["lines"]
    assert len(lines) == 3
    assert all(line.strip() for line in lines)
    assert lines[1].endswith(("。", "！", "？"))
    assert lines[2].endswith(("。", "！", "？"))


def test_one_day_report_warns_insufficient_data(client, shop):
    """施工单 3.6：只有 1 天数据 → 出指标，结论加「数据不足 7 天，仅供参考」。"""
    client.put(
        "/api/daily",
        json={
            "shop_id": shop.id,
            "stat_date": "2026-10-01",
            "values": {"revenue": 4200, "purchase": 1100, "orders": 140},
            "shifts": [{"worker_name": "员工A", "start_time": "18:00", "end_time": "22:00"}],
        },
    )
    data = _report(client, shop.id)
    warnings = data["conclusion"]["warnings"]
    assert any("数据不足 7 天" in w for w in warnings), warnings

    values = {m["code"]: m["value"] for m in data["metrics"]}
    assert values["revenue"] == pytest.approx(4200)   # 一天也照出指标
    assert values["avg_ticket"] == pytest.approx(30.0)


def test_conclusion_has_no_jargon(client, sample_json, shop):
    """话术护栏：绝不出现财务术语（施工单 1.4）。"""
    _push_sample_over_http(client, shop.id, sample_json)
    data = _report(client, shop.id)

    blobs = list(data["conclusion"]["lines"])
    blobs += data["conclusion"]["warnings"]
    blobs += [m["sentence"] for m in data["metrics"] if m.get("sentence")]
    known_codes = {m["code"] for m in data["metrics"]}
    blobs += [m["name"] for m in data["metrics"] if m.get("name") and m["code"] not in known_codes]

    for blob in blobs:
        for term in FORBIDDEN_TERMS:
            assert term not in blob, f"结论里出现了财务术语 {term!r}：{blob}"


def test_report_mentions_breakeven_when_below(client, sample_json, shop):
    """日均低于保本线时，第二行必须直接说这件事（这是产品的核心价值）。"""
    _push_sample_over_http(client, shop.id, sample_json)
    # 把房租抬到日均一定够不到的高度
    client.put(f"/api/shops/{shop.id}/monthly", json={"month": "2026-10", "values": {"rent_m": 60000}})
    data = _report(client, shop.id)
    assert "才不亏" in data["conclusion"]["lines"][1]


def test_weekly_endpoint(client, sample_json, shop):
    _push_sample_over_http(client, shop.id, sample_json)
    res = client.get(
        "/api/report/weekly",
        params={"shop_id": shop.id, "anchor": PERIOD_END.isoformat(), "days": 7},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["period"]["start"] == PERIOD_START.isoformat()
    assert body["period"]["end"] == PERIOD_END.isoformat()
    assert body["period"]["days_with_data"] == 7


def test_observations_split_weekend_and_weekday(client, sample_json, shop):
    _push_sample_over_http(client, shop.id, sample_json)
    obs = _report(client, shop.id)["observations"]
    assert obs["total_revenue"] == pytest.approx(EXPECTED["revenue"], abs=0.01)
    assert obs["daily_avg_revenue"] == pytest.approx(23000 / 7, abs=0.01)
    assert len(obs["daily"]) == 7
    # 2026-10-03/04 是周六周日
    weekend = [d for d in obs["daily"] if d["is_weekend"]]
    assert len(weekend) == 2
    assert obs["weekend_avg_revenue"] is not None


# =========================================================================== #
# 4. 蓝图 v1.0 §2.2 的核心诉求：加字段 = 插元数据一行，**不执行任何 DDL**
# =========================================================================== #
def test_add_field_without_ddl_then_use_it(client, session, shop):
    """照抄蓝图 v1.0 §2.2 的原例：加「堂食客单价」。

    该节原文："加「堂食客单价」这个字段的完整流程 = 往 field_def 插 1 行
    + 往 metric_def 插 1 行。**不执行任何 DDL。**"

    所以这个测试里**一条 ALTER TABLE 都不许有**，也不许改 Python / 前端。
    """
    before = {f["code"] for f in client.get("/api/meta/fields").json()}
    assert "revenue_dine" not in before

    # ---- 唯一动作：两条 INSERT ----
    session.execute(
        text(
            "INSERT INTO field_def (code, entity, label, data_type, unit, scope,"
            " is_required, default_value, enum_values, sort_order, enabled, help_text)"
            " VALUES ('revenue_dine', 'fact_daily', '堂食营业额', 'number', '元', 'day',"
            " 0, NULL, NULL, 4, 1, '堂食收了多少')"
        )
    )
    session.execute(
        text(
            "INSERT INTO field_def (code, entity, label, data_type, unit, scope,"
            " is_required, default_value, enum_values, sort_order, enabled, help_text)"
            " VALUES ('orders_dine', 'fact_daily', '堂食订单数', 'integer', '单', 'day',"
            " 0, NULL, NULL, 5, 1, '堂食多少单')"
        )
    )
    session.execute(
        text(
            "INSERT INTO metric_def (code, name, layer, unit, formula, depends_on,"
            " benchmark_key, warn_high, warn_low, higher_better, plain_text, enabled,"
            " version, aggregation, scope, inputs)"
            " VALUES ('avg_ticket_dine', '堂食客单价', 'derived', '元',"
            " 'revenue_dine / orders_dine', NULL, NULL, NULL, NULL, 1,"
            " '堂食平均每单 {value} 元', 1, 1, 'ratio', 'day', NULL)"
        )
    )
    session.commit()

    # ---- 不改代码，直接就能录 ----
    res = client.put(
        "/api/daily",
        json={
            "shop_id": shop.id,
            "stat_date": "2026-10-01",
            "values": {"revenue": 4200, "orders": 140, "revenue_dine": 2600, "orders_dine": 65},
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["values"]["revenue_dine"] == pytest.approx(2600)

    # ---- 前端 meta 直接能看到，表单自己会长出输入框 ----
    day_fields = {f["code"] for f in client.get("/api/meta/fields", params={"scope": "day"}).json()}
    assert {"revenue_dine", "orders_dine"} <= day_fields

    # ---- 引擎自己就会算，报告自己会带 ----
    data = _report(client, shop.id)
    values = {m["code"]: m["value"] for m in data["metrics"]}
    assert values["avg_ticket_dine"] == pytest.approx(40.0)

    new_metric = next(m for m in data["metrics"] if m["code"] == "avg_ticket_dine")
    assert new_metric["name"] == "堂食客单价"
    assert new_metric["sentence"] == "堂食平均每单 40 元"   # 整数优先，不硬凑两位小数

    # ---- 老指标没被影响 ----
    assert values["avg_ticket"] == pytest.approx(30.0)


def test_weekly_report_never_averages_daily_rates(client, shop):
    """蓝图核验窗口**点名**的场景：M2 加周报时图省事对"日比率取平均"。

    它原话："这条守卫是整套架构里最容易在后续被无意破坏的地方"。所以这里
    从 HTTP 周报入口再打一遍，并且把两条路径的差距拉到数量级。

        两天数据：D1 营业额 100、工时 10   → 当日人力成本率 250%
                 D2 营业额 300、工时 2    → 当日人力成本率 16.67%
        正确（先 Σ 原子）: 300 / 400       = 75.00%
        错误（对日率取平均）: (250%+16.67%)/2 ≈ 133.33%

        人效同理：正确 400/12 = 33.33；错误 (10+150)/2 = 80
    """
    for stat_date, revenue, start, end in (
        ("2026-10-01", 100, "10:00", "20:00"),   # 10 小时
        ("2026-10-02", 300, "10:00", "12:00"),   # 2 小时
    ):
        res = client.put(
            "/api/daily",
            json={
                "shop_id": shop.id,
                "stat_date": stat_date,
                "values": {"revenue": revenue},
                "shifts": [
                    {"worker_name": "员工A", "start_time": start, "end_time": end},
                ],
            },
        )
        assert res.status_code == 200, res.text

    res = client.get(
        "/api/report/weekly",
        params={"shop_id": shop.id, "anchor": "2026-10-02", "days": 2},
    )
    assert res.status_code == 200, res.text
    values = {m["code"]: m["value"] for m in res.json()["metrics"]}

    assert values["hours"] == pytest.approx(12.0)          # 10 + 2（小时可加）
    assert values["labor_cost"] == pytest.approx(300.0)    # 12h × 25

    # 走对的那条路
    assert values["labor_cost_rate"] == pytest.approx(0.75, abs=0.001)
    assert values["efficiency"] == pytest.approx(400 / 12, abs=0.01)

    # 明确排除"对日率取平均"那条错误路径
    assert abs(values["labor_cost_rate"] - 1.3333) > 0.1
    assert abs(values["efficiency"] - 80.0) > 1.0


def _report_range(client, shop_id: int, start: str, end: str) -> dict:
    res = client.get(
        "/api/report", params={"shop_id": shop_id, "start": start, "end": end}
    )
    assert res.status_code == 200, res.text
    return res.json()


def _seed_period(
    client,
    shop_id: int,
    start_iso: str,
    days: int,
    *,
    revenue: float,
    food: float,
    start_time: str = "09:00",
    end_time: str = "17:00",
    hourly_rate: float = 25.0,
) -> None:
    """连续灌 N 天：每天同样的营业额/食材比/排班。用于造"等长区间"做环比测试。"""
    from datetime import date as _date, timedelta as _td

    d0 = _date.fromisoformat(start_iso)
    for i in range(days):
        res = client.put(
            "/api/daily",
            json={
                "shop_id": shop_id,
                "stat_date": (d0 + _td(days=i)).isoformat(),
                "values": {
                    "revenue": revenue,
                    "purchase": round(revenue * food, 2),
                    "orders": 40,
                },
                "shifts": [
                    {
                        "worker_name": "员工A",
                        "start_time": start_time,
                        "end_time": end_time,
                        "hourly_rate": hourly_rate,
                    }
                ],
            },
        )
        assert res.status_code == 200, res.text


# =========================================================================== #
# 6. M2-0 · 基准区间修正 + 话术与区间不许脱钩
# =========================================================================== #
def test_food_cost_rate_new_range_verdicts():
    """docs/02 §2 M2-0 判据：新区间 28%–38%（预警 >40%）。

    旧区间 18%–22% 的后果是**所有**快餐店都被判"偏高" → 狼来了 →
    老板第三次看到预警就不信了。所以 30% 必须判 ok。
    """
    from server import benchmarks as bm

    assert bm.compare("catering", "food_cost_rate", 0.30)["status"] == "ok"    # Y1
    assert bm.compare("catering", "food_cost_rate", 0.36)["status"] == "ok"    # Y2
    assert bm.compare("catering", "food_cost_rate", 0.27)["status"] == "low"
    assert bm.compare("catering", "food_cost_rate", 0.40)["status"] == "high"  # 预警线
    assert bm.compare("catering", "food_cost_rate", 0.41)["status"] == "high"  # Y3
    assert bm.benchmark_for("catering", "food_cost_rate")["display"] == "28%–38%"


def test_benchmark_text_matches_range():
    """**这次的错误就是"改了区间忘了改话术"**，所以用正则从 display 反查文案。

    规则：
      1. display 里出现过的百分比，才是话术里允许出现的百分比 —— 出现别的就红；
      2. 判"偏高"的话术必须把区间说给老板听（否则他不知道该往哪调）；
      3. display 与 low/high 必须自洽（改了数值忘了改 display 也要红）。
    """
    import re

    from server import benchmarks as bm

    pct = re.compile(r"\d+(?:\.\d+)?%")
    text_fields = ("advice_high", "advice_low", "advice_ok", "action_high", "action_low", "action_ok")

    checked = 0
    for category, table in bm.BENCHMARKS.items():
        for key, bench in table.items():
            text_blob = " ".join(str(bench.get(f) or "") for f in text_fields)
            display = bench.get("display")

            if not display or "%" not in display:
                # 非百分比区间（客单价/人效/保本）也要保证话术里没有百分比，免得串档
                assert not pct.search(text_blob), f"{category}.{key} 话术里不该出现百分比：{text_blob}"
                continue

            checked += 1
            allowed = set(pct.findall(display))
            for field in text_fields:
                found = set(pct.findall(str(bench.get(field) or "")))
                extra = found - allowed
                assert not extra, (
                    f"{category}.{key}.{field} 里出现了不在区间 {display} 中的百分比 {sorted(extra)} —— "
                    f"改了区间没改话术（docs/02 M2-0 就是栽在这里）"
                )
            assert any(tok in str(bench.get("advice_high") or "") for tok in allowed), (
                f"{category}.{key}.advice_high 没把区间 {display} 说清楚，老板不知道该往哪调"
            )

            # display 与 low/high 自洽
            bounds = {f"{bench['low']:.0%}", f"{bench['high']:.0%}"}
            assert bounds == allowed, (
                f"{category}.{key} 的 display {display} 与 low/high {sorted(bounds)} 不一致"
            )

    assert checked >= 2, "至少应该检查到食材成本率和人力成本率两个百分比区间"


def test_every_range_has_source():
    """M2-0 硬要求：**禁止凭感觉填区间**，每个区间必须写清来源。

    没查证过的就写"待查证"，不许编。
    """
    from server import benchmarks as bm

    for category, table in bm.BENCHMARKS.items():
        for key, bench in table.items():
            source = (bench.get("source") or "").strip()
            assert source, f"{category}.{key} 缺 source：基准值必须写清从哪来"
            assert len(source) >= 8, f"{category}.{key} 的 source 太短，等于没写"

    food = bm.benchmark_for("catering", "food_cost_rate")
    assert "待查证" not in food["source"], "食材成本率是 M2-0 明确查证过的，不该还挂着待查证"
    assert "2026-10" in food["source"] or "docs/01" in food["source"]


# =========================================================================== #
# 7. M2-1 · 全口径人工
# =========================================================================== #
def test_labor_m_full_degrades_to_labor_m_when_new_fields_missing(client, shop):
    """docs/02 §2 M2-1 判据 3：某月只填了房租、没填全职月薪时，
    `labor_m_full` 必须**退化为 labor_m** —— 不许返回 None 或 0 把保本线拉低。
    """
    client.put(
        f"/api/shops/{shop.id}/monthly",
        json={"month": "2026-10", "values": {"rent_m": 6000, "utility_m": 1500, "gross_margin": 0.6}},
    )
    client.put(
        "/api/daily",
        json={
            "shop_id": shop.id,
            "stat_date": "2026-10-05",
            "values": {"revenue": 4000},
            "shifts": [{"worker_name": "全职A", "start_time": "09:00", "end_time": "17:00"}],
        },
    )
    data = _report_range(client, shop.id, "2026-10-01", "2026-10-31")
    values = {m["code"]: m["value"] for m in data["metrics"]}

    assert values["labor_m"] == pytest.approx(200.0)
    assert values["labor_m_full"] == pytest.approx(200.0), "应退化为 labor_m，不是 None / 0"
    assert values["breakeven_daily"] is not None
    assert values["breakeven_daily"] > 0


def test_labor_m_full_adds_social_insurance_and_raises_breakeven(client, shop):
    """M2-1 的核心价值：把社保/公积金/福利这三笔"老板通常没算的钱"加进保本线。"""
    client.put(
        f"/api/shops/{shop.id}/monthly",
        json={"month": "2026-10", "values": {"rent_m": 6000, "utility_m": 1500, "gross_margin": 0.6}},
    )
    client.put(
        "/api/daily",
        json={
            "shop_id": shop.id,
            "stat_date": "2026-10-05",
            "values": {"revenue": 4000},
            "shifts": [{"worker_name": "全职A", "start_time": "09:00", "end_time": "17:00"}],
        },
    )
    before = _report_range(client, shop.id, "2026-10-01", "2026-10-31")
    be_before = {m["code"]: m["value"] for m in before["metrics"]}["breakeven_daily"]

    client.put(
        f"/api/shops/{shop.id}/monthly",
        json={
            "month": "2026-10",
            "values": {
                "social_insurance_m": 4800,
                "housing_fund_m": 1000,
                "welfare_m": 400,
            },
        },
    )
    after = _report_range(client, shop.id, "2026-10-01", "2026-10-31")
    values = {m["code"]: m["value"] for m in after["metrics"]}
    be_after = values["breakeven_daily"]

    assert values["labor_m_full"] == pytest.approx(200.0 + 4800 + 1000 + 400)   # labor_m + 三笔新钱
    assert values["labor_m"] == pytest.approx(200.0)             # 排班工钱不受影响
    assert be_after > be_before, "社保公积金福利进了公式，保本线必须变高"

    # 差额要能被报告说出来（docs/02 M2-1 报告口径要求）
    labor = after["observations"]["labor"]
    assert labor["gap"] == pytest.approx(6200.0)
    assert "月口径" in labor["note"]
    assert "没算进去的钱" in after["conclusion"]["lines"][0]


# =========================================================================== #
# 8. M2-2 话术分档 / M2-4 保本自比对
# =========================================================================== #
def test_year_tier_conclusion_never_says_tomorrow(client, shop):
    """docs/02 §3 M2-2 硬判据：年度档第三行不得包含"明天"。"""
    _seed_period(client, shop.id, "2026-01-05", 3, revenue=1000, food=0.41)
    data = _report_range(client, shop.id, "2026-01-01", "2027-01-01")

    assert data["period"]["days_span"] == 366
    assert data["period"]["tier"] == "year"
    lines = data["conclusion"]["lines"]
    assert len(lines) == 3
    assert "明天" not in lines[2], f"年度档第三行还在说明天：{lines[2]}"
    assert "明年" in lines[2]
    assert "这一年" in lines[1] or "保本" in lines[1]


def test_week_tier_conclusion_still_says_tomorrow(client, shop):
    """不许误伤：7 天档第三行必须仍然是"明天做什么"。"""
    _seed_period(client, shop.id, "2026-01-05", 7, revenue=1000, food=0.41)
    data = _report_range(client, shop.id, "2026-01-05", "2026-01-11")

    assert data["period"]["days_span"] == 7
    assert data["period"]["tier"] == "day"
    assert "明天" in data["conclusion"]["lines"][2]


def test_month_tier_conclusion_uses_period_tone(client, shop):
    _seed_period(client, shop.id, "2026-03-01", 10, revenue=1000, food=0.41)
    data = _report_range(client, shop.id, "2026-03-01", "2026-04-15")

    assert data["period"]["days_span"] == 46
    assert data["period"]["tier"] == "month"
    lines = data["conclusion"]["lines"]
    assert "明天" not in lines[2]
    assert "这个月" in lines[2]


def test_breakeven_self_compare_only_in_year_tier():
    """M2-4：保本额没有同行区间，只能自比 —— 且只在年度档说，避免周报里添噪音。"""
    from server.routers.report import _breakeven_self_compare
    from server.schemas import TrendItem

    trend = [
        TrendItem(
            code="breakeven_daily",
            name="保本营业额(日)",
            current=2598.68,
            previous=1510.71,
            delta=1087.97,
            direction="升",
        )
    ]
    obs = {"daily_avg_revenue": 4705.84, "prev_daily_avg_revenue": 3717.05}

    year_line, year_action = _breakeven_self_compare(trend, obs, "year")
    assert year_line and "保本" in year_line
    assert "72%" in year_line        # 保本线涨幅（1087.97 / 1510.71 ≈ 72%）
    # 日均营业额涨幅（(4705.84/3717.05 - 1) ≈ 26.6% → 取整 27%）
    rev_pct = round((obs["daily_avg_revenue"] / obs["prev_daily_avg_revenue"] - 1) * 100)
    assert f"{rev_pct}%" in year_line
    assert "你越做越危险" in year_line
    assert year_action and "明年" in year_action

    assert _breakeven_self_compare(trend, obs, "day")[0] is None
    assert _breakeven_self_compare(trend, obs, "month")[0] is None


def test_no_breakeven_compare_when_no_trend():
    from server.routers.report import _breakeven_self_compare

    assert _breakeven_self_compare([], {}, "year")[0] is None


# =========================================================================== #
# 9. M2-3 · 环比段
# =========================================================================== #
def test_trend_directions_and_deltas(client, shop):
    """docs/02 §3 M2-3 判据：食材 +0.06 升、+0.05 升；人效 Y3 降。"""
    _seed_period(client, shop.id, "2026-01-01", 10, revenue=1000, food=0.30,
                 start_time="08:00", end_time="16:00")   # 8 小时
    _seed_period(client, shop.id, "2026-01-11", 10, revenue=1000, food=0.36,
                 start_time="08:00", end_time="16:00")
    _seed_period(client, shop.id, "2026-01-21", 10, revenue=1000, food=0.41,
                 start_time="08:00", end_time="20:00")   # 12 小时 → 人效掉

    p2 = _report_range(client, shop.id, "2026-01-11", "2026-01-20")
    food2 = next(t for t in p2["trend"] if t["code"] == "food_cost_rate")
    assert food2["direction"] == "升"
    assert food2["delta"] == pytest.approx(0.06, abs=0.001)
    assert food2["previous"] == pytest.approx(0.30, abs=0.001)
    assert food2["current"] == pytest.approx(0.36, abs=0.001)
    assert "在涨" in food2["sentence"] and "30%" in food2["sentence"] and "36%" in food2["sentence"]

    p3 = _report_range(client, shop.id, "2026-01-21", "2026-01-30")
    food3 = next(t for t in p3["trend"] if t["code"] == "food_cost_rate")
    assert food3["direction"] == "升"
    assert food3["delta"] == pytest.approx(0.05, abs=0.001)

    eff3 = next(t for t in p3["trend"] if t["code"] == "efficiency")
    assert eff3["direction"] == "降", "Y3 工时不减、营业额不涨，人效必须判降"
    assert eff3["current"] < eff3["previous"]

    # 营业额环比也在
    rev3 = next(t for t in p3["trend"] if t["code"] == "revenue")
    assert rev3["direction"] == "平"


def test_trend_empty_on_first_period(client, shop):
    """docs/02 §3 M2-3 判据：首期（无上一期）trend 为空列表，且不报错。"""
    _seed_period(client, shop.id, "2026-01-01", 10, revenue=1000, food=0.30)
    data = _report_range(client, shop.id, "2026-01-01", "2026-01-10")
    assert data["trend"] == []
    assert len(data["conclusion"]["lines"]) == 3


def test_trend_never_averages_daily_ratios(client, shop):
    """M2-3 硬纪律：环比必须先 Σ 原子再算比率，不得对日率取平均。

    造一个"两条路径差一个数量级"的两天区间：

        上期  D1 营业 100 / 工时 10 → 当日率 250%
              D2 营业 300 / 工时 2  → 当日率 16.67%
              正确（Σ/Σ）: 300 / 400 = 75%      错误（对日率取平均）: ≈133.33%
    """
    # 用"固定 08:00 起、工时长 = 结束 - 开始"来精确控制工时
    def put(stat_date: str, revenue: float, end_time: str) -> None:
        res = client.put(
            "/api/daily",
            json={
                "shop_id": shop.id,
                "stat_date": stat_date,
                "values": {"revenue": revenue},
                "shifts": [
                    {"worker_name": "员工A", "start_time": "08:00", "end_time": end_time,
                     "hourly_rate": 25.0}
                ],
            },
        )
        assert res.status_code == 200, res.text

    # 上期：D1 10h（08:00→18:00）、D2 2h（08:00→10:00）
    put("2026-02-01", 100, "18:00")
    put("2026-02-02", 300, "10:00")
    # 本期：随便给两天数据，只要存在上一期
    put("2026-02-03", 200, "18:00")
    put("2026-02-04", 600, "10:00")

    data = _report_range(client, shop.id, "2026-02-03", "2026-02-04")
    item = next(t for t in data["trend"] if t["code"] == "labor_cost_rate")

    # 上期：labor_cost = 10h×25 + 2h×25 = 300，revenue = 400 → 75%
    assert item["previous"] == pytest.approx(0.75, abs=0.001)
    # 明确排除"对日率取平均"那条错误路径（(250% + 16.67%)/2 ≈ 133%）
    assert abs(item["previous"] - 1.3333) > 0.1

    # 本期：同样两人两班 → 12h，labor = 300；revenue 800 → 37.5%
    assert item["current"] == pytest.approx(0.375, abs=0.001)
    assert item["direction"] == "降"


# =========================================================================== #
# 11. M3-0 · 全口径人力成本率（第 6 个派生指标）
# =========================================================================== #
YEAR_SPAN = ("2025-04-01", "2026-03-31")   # 365 天、年度档；数据灌在 2026-03（正好是锚点月）


def test_labor_cost_rate_full_scope_three_ways_agree(client, shop):
    """M3-0 要求「**必须在测试里断言口径**」。

    任务书两个方案数学上完全等价，我用的写法（把 `revenue_avg × 30` 预算成
    `revenue_m_avg`）也与它们等价：

        (a) labor_m_full / (revenue_avg × 30)
        (b) (labor_m_full / 30) / revenue_avg
        (c) labor_m_full / revenue_m_avg        ← 实际实现

    ⚠️ 这条测试的关键设计：**两个月的日均营业额故意不一样**（锚点月 3000/天、
    另一月 6000/天），于是
        锚点月口径：9000 / 90000  = 10.0%
        区间日均口径：9000 / 135000 = 6.67%   ← 任务书要的是这个
    两者可区分，才能证明跨口径处理没写错。若两个月的日均恰好相等，写错也测不出来。
    """
    # 锚点月（2026-03）：3000/天 × 30 天 = 90000，排班 100/天 → 月 3000
    _seed_period(client, shop.id, "2026-03-01", 30, revenue=3000, food=0.30,
                 start_time="08:00", end_time="12:00")
    # 非锚点月（2025-09）：6000/天 × 30 天 = 180000（拉高区间日均，不与锚点月混同）
    _seed_period(client, shop.id, "2025-09-01", 30, revenue=6000, food=0.30,
                 start_time="08:00", end_time="12:00")
    client.put(
        f"/api/shops/{shop.id}/monthly",
        json={"month": "2026-03", "values": {"social_insurance_m": 6000}},
    )
    data = _report_range(client, shop.id, *YEAR_SPAN)
    v = {m["code"]: m["value"] for m in data["metrics"]}

    assert v["revenue"] == pytest.approx(270000.0)        # 区间总额
    assert v["revenue_m"] == pytest.approx(90000.0)       # 锚点月实际（利润类指标用）
    assert v["labor_m"] == pytest.approx(3000.0)          # 只算锚点月的排班
    assert v["labor_m_full"] == pytest.approx(9000.0)     # 排班工钱 + 社保

    # 月化营业额 = 区间日均 × 30 = (270000/60) × 30
    rev_avg = 270000 / 60
    assert v["revenue_m_avg"] == pytest.approx(rev_avg * 30)
    assert v["revenue_m_avg"] == pytest.approx(135000.0)

    # 引擎把结果 round 到 6 位小数，所以这里的容差要容得下那一级舍入
    assert v["labor_cost_rate_full"] == pytest.approx(9000 / 135000, abs=1e-6)
    # 三种写法同值
    assert v["labor_cost_rate_full"] == pytest.approx(v["labor_m_full"] / v["revenue_m_avg"], abs=1e-6)
    assert v["labor_cost_rate_full"] == pytest.approx(v["labor_m_full"] / (rev_avg * 30), abs=1e-6)
    assert v["labor_cost_rate_full"] == pytest.approx((v["labor_m_full"] / 30) / rev_avg, abs=1e-6)
    # 明确排除"拿锚点月营业额当分母"那条错路（9000/90000 = 10%）
    assert abs(v["labor_cost_rate_full"] - 0.10) > 0.02


def test_labor_cost_rate_full_uses_same_benchmark_as_direct_rate(client, shop):
    """M3-0 要求 benchmark_key 复用 labor_cost_rate 的区间。"""
    from server import benchmarks as bm

    _seed_period(client, shop.id, "2026-03-01", 30, revenue=3000, food=0.30,
                 start_time="08:00", end_time="12:00")
    client.put(
        f"/api/shops/{shop.id}/monthly",
        json={"month": "2026-03", "values": {"social_insurance_m": 6000}},
    )
    data = _report_range(client, shop.id, *YEAR_SPAN)
    metric = next(m for m in data["metrics"] if m["code"] == "labor_cost_rate_full")
    assert metric["benchmark"] is not None
    assert metric["benchmark"]["range_display"] == bm.benchmark_for("catering", "labor_cost_rate")["display"]
    assert metric["name"] == "全口径人力成本率"
    assert "社保" in (metric["sentence"] or "")


# =========================================================================== #
# 12. M3-1 · 基准区间收敛
# =========================================================================== #
def test_m3_benchmark_ranges_and_verdicts():
    """docs/03 §2 判据：**Y3 人力 21.2% 必须判 high** —— 没变 high 就是没改对。"""
    from server import benchmarks as bm

    # 人力成本率 15%–20%
    assert bm.compare("catering", "labor_cost_rate", 0.161)["status"] == "ok"     # Y1
    assert bm.compare("catering", "labor_cost_rate", 0.166)["status"] == "ok"     # Y2
    assert bm.compare("catering", "labor_cost_rate", 0.212)["status"] == "high"   # Y3 ← 关键判据
    assert bm.compare("catering", "labor_cost_rate", 0.20)["status"] == "ok"
    assert bm.compare("catering", "labor_cost_rate", 0.201)["status"] == "high"
    assert bm.compare("catering", "labor_cost_rate", 0.14)["status"] == "low"
    assert bm.benchmark_for("catering", "labor_cost_rate")["display"] == "15%–20%"

    # 人效 120–180
    assert bm.compare("catering", "efficiency", 128.36)["status"] == "ok"   # Y1
    assert bm.compare("catering", "efficiency", 153.35)["status"] == "ok"   # Y2
    assert bm.compare("catering", "efficiency", 123.88)["status"] == "ok"   # Y3，贴近下限
    assert bm.compare("catering", "efficiency", 119.9)["status"] == "low"
    assert bm.compare("catering", "efficiency", 180.1)["status"] == "high"
    assert bm.benchmark_for("catering", "efficiency")["display"] == "120–180 元/小时"

    # 客单价保留 15–45
    assert bm.benchmark_for("catering", "avg_ticket")["display"] == "15–45 元"


def test_no_pending_source_left_after_m3_1():
    """M3-1 之后**不许再有"待查证"** —— 评审窗口已经把欠的查证补上了。"""
    from server import benchmarks as bm

    for category, table in bm.BENCHMARKS.items():
        for key, bench in table.items():
            source = str(bench.get("source") or "")
            assert "待查证" not in source, f"{category}.{key} 还挂着「待查证」"
            assert "2026-10" in source or "无同行区间" in source, f"{category}.{key} 缺查证日期"

    ticket_source = bm.benchmark_for("catering", "avg_ticket")["source"]
    assert "12–46" in ticket_source or "12-46" in ticket_source, "客单价必须写明跨度为什么收不窄"
    assert "细分业态" in ticket_source


# =========================================================================== #
# 13. M3-2 · 隐性成本：账面 vs 实际
# =========================================================================== #
def _seed_hidden_costs(client, shop, **overrides) -> None:
    values = {
        "rent_m": 6000,
        "utility_m": 1500,
        "full_time_monthly": 20000,
        "social_insurance_m": 4800,
        "housing_fund_m": 1000,
        "welfare_m": 400,
        "staff_meal_m": 2000,
        "tableware_m": 500,
        "repair_m": 800,
        "cash_diff_m": 400,
        "returned_m": 300,
    }
    values.update(overrides)
    client.put(f"/api/shops/{shop.id}/monthly", json={"month": "2026-03", "values": values})


def test_hidden_cost_gap_spoken_in_conclusion(client, shop):
    """docs/03 §3 M3-2 判据：3 年数据补录隐性成本后，实际利润必须**显著低于**账面利润，
    且差额在结论里说出来。
    """
    _seed_period(client, shop.id, "2026-03-01", 30, revenue=10000, food=0.35,
                 start_time="08:00", end_time="16:00")   # 8h×25 = 200/天
    _seed_hidden_costs(client, shop)

    data = _report_range(client, shop.id, *YEAR_SPAN)
    v = {m["code"]: m["value"] for m in data["metrics"]}

    # 隐性成本 = 2000 + 500 + 800 + 400 + 300
    assert v["hidden_cost_m"] == pytest.approx(4000.0)
    # 账面利润 = 300000 − 105000 − 32200 − 6000 − 1500
    assert v["book_profit_m"] == pytest.approx(300000 - 105000 - 32200 - 6000 - 1500)
    assert v["real_profit_m"] == pytest.approx(v["book_profit_m"] - 4000.0)
    assert v["real_profit_m"] < v["book_profit_m"], "实际利润必须低于账面利润"

    line1 = data["conclusion"]["lines"][0]
    assert "账面赚了" in line1
    assert "实际还剩" in line1
    assert "中间差 4,000" in line1, line1
    assert "员工餐" in line1 and "收银差异" in line1


def test_hidden_cost_silent_when_nothing_filled(client, shop):
    """没填隐性成本时不许编差额、也不许说话。"""
    _seed_period(client, shop.id, "2026-03-01", 30, revenue=10000, food=0.35,
                 start_time="08:00", end_time="16:00")
    data = _report_range(client, shop.id, *YEAR_SPAN)
    v = {m["code"]: m["value"] for m in data["metrics"]}
    assert v["hidden_cost_m"] == pytest.approx(0.0)   # default_value="0"
    assert "中间差" not in data["conclusion"]["lines"][0]


# =========================================================================== #
# 14. M3-3 · 账实差异
# =========================================================================== #
def _make_goods(client, shop, code="YOU", name="色拉油", unit="斤", price=30.0,
                category_code="02-01", **extra) -> int:
    body = {
        "shop_id": shop.id,
        "code": code,
        "name": name,
        "unit": unit,
        "unit_price": price,
        "category_code": category_code,
        "confirmed_not_duplicate": True,
    }
    body.update(extra)
    res = client.post("/api/goods", json=body)
    assert res.status_code == 201, res.text
    return res.json()["id"]


def test_stock_variance_rate_amount_and_sentence(client, shop):
    """docs/03 §3 M3-3 判据：账面 100 / 实盘 93 → 差异率 7%、金额正确、结论说出来。"""
    _seed_period(client, shop.id, "2026-03-01", 3, revenue=5000, food=0.35)
    goods_id = _make_goods(client, shop, price=30.0)

    res = client.put(
        "/api/stocktakes",
        json={"shop_id": shop.id, "goods_id": goods_id, "take_date": "2026-03-02",
              "book_qty": 100, "actual_qty": 93},
    )
    assert res.status_code == 200, res.text
    assert res.json()["unit_price"] == pytest.approx(30.0)   # 主数据快照

    data = _report_range(client, shop.id, "2026-03-01", "2026-03-31")
    v = {m["code"]: m["value"] for m in data["metrics"]}

    assert v["stock_book_amount"] == pytest.approx(3000.0)      # 100 × 30
    assert v["stock_actual_amount"] == pytest.approx(2790.0)    # 93 × 30
    assert v["stock_variance_amount"] == pytest.approx(-210.0)
    assert v["stock_variance_rate"] == pytest.approx(0.07)      # |−7| / 100

    line2 = data["conclusion"]["lines"][1]
    assert "账面应该剩 100斤" in line2
    assert "实际只有 93斤" in line2
    assert "差 7斤" in line2
    assert "210 元" in line2

    detail = data["observations"]["stock_variance"]
    assert detail["has_data"] is True
    assert detail["items"][0]["variance_qty"] == pytest.approx(-7.0)
    # 账面由系统推算，说明文字要讲清公式是什么
    assert "上期实盘" in detail["note"] and "Σ入库" in detail["note"]
    # ⚠️ 这条断言在 M5-0 之前写的是"必须标注**仍缺**领用（bom 未解锁）"。
    # docs/05 §3 解锁 bom 之后，领用已经进公式了 —— 语义**反过来了**，
    # 所以改成下面三条（不是删断言，是把"缺一块"换成"这块补上了"）：
    assert "Σ理论领用" in detail["note"], "M5-0 之后账面公式必须含领用"
    assert "跑冒滴漏" in detail["note"], "补上领用后才能说差异=跑冒滴漏"
    assert "成本卡" in detail["note"], "必须提示这个数依赖成本卡准不准，不能假装无前提"


def test_stock_variance_says_nothing_when_zero(client, shop):
    """差异为 0 时**不出这句话**（任务书：不许制造噪音）。"""
    _seed_period(client, shop.id, "2026-03-01", 3, revenue=5000, food=0.35)
    goods_id = _make_goods(client, shop, price=30.0)
    client.put(
        "/api/stocktakes",
        json={"shop_id": shop.id, "goods_id": goods_id, "take_date": "2026-03-02",
              "book_qty": 100, "actual_qty": 100},
    )
    data = _report_range(client, shop.id, "2026-03-01", "2026-03-31")
    v = {m["code"]: m["value"] for m in data["metrics"]}

    assert v["stock_variance_rate"] == pytest.approx(0.0)
    assert data["observations"]["stock_variance"]["sentence"] is None
    assert "账面应该剩" not in " ".join(data["conclusion"]["lines"])


def test_stock_variance_silent_without_any_stocktake(client, shop):
    """没盘点过 → 不返回 0，而是 None（不让报告说"差异 0%"这种假安心的话）。"""
    _seed_period(client, shop.id, "2026-03-01", 3, revenue=5000, food=0.35)
    data = _report_range(client, shop.id, "2026-03-01", "2026-03-31")
    v = {m["code"]: m["value"] for m in data["metrics"]}
    assert v["stock_variance_rate"] is None
    assert data["observations"]["stock_variance"]["has_data"] is False


def test_scraps_and_units_lost_recorded(client, shop):
    """报废单能记"丢了几单"，用来把差异拆成"自己报损的"和"说不清的"。"""
    _seed_period(client, shop.id, "2026-03-01", 3, revenue=5000, food=0.35)
    goods_id = _make_goods(client, shop, price=30.0)
    # M5-2：原因必须代码化（T02 存储变质）。自由文本"倒掉"不能聚合，已被拒绝
    res = client.post(
        "/api/scraps",
        json={"shop_id": shop.id, "goods_id": goods_id, "scrap_date": "2026-03-02",
              "qty": 2, "units_lost": 1, "reason_code": "T02", "reason": "倒掉"},
    )
    assert res.status_code == 201, res.text
    assert res.json()["reason_code"] == "T02"
    assert res.json()["amount"] == pytest.approx(60.0)
    data = _report_range(client, shop.id, "2026-03-01", "2026-03-31")
    v = {m["code"]: m["value"] for m in data["metrics"]}
    assert v["scrap_amount"] == pytest.approx(60.0)   # 2 × 30


# =========================================================================== #
# 15. M3-6 · 录入约定防呆（告警不是报错）
# =========================================================================== #
def test_double_count_warning_when_both_conventions_used(client, shop):
    """同时填全职月薪 + 给全职记排班 → 告警，且请求本身必须成功（不是报错）。"""
    _seed_hidden_costs(client, shop, staff_meal_m=0, tableware_m=0, repair_m=0,
                       cash_diff_m=0, returned_m=0)
    res = client.put(
        "/api/daily",
        json={
            "shop_id": shop.id,
            "stat_date": "2026-03-02",
            "values": {"revenue": 5000},
            "shifts": [{"worker_name": "全职A", "start_time": "08:00", "end_time": "16:00"}],
        },
    )
    assert res.status_code == 200, "防呆是告警不是报错，请求必须成功"

    data = _report_range(client, shop.id, "2026-03-01", "2026-03-31")
    warnings = data["conclusion"]["warnings"]
    assert any("重复计算" in w for w in warnings), warnings
    alerts = data["observations"]["input_alerts"]
    assert alerts and alerts[0]["code"] == "labor_double_count_risk"
    assert alerts[0]["level"] == "warn"


def test_no_double_count_warning_without_full_time_monthly(client, shop):
    """只给小时工记排班、没填全职月薪 → 不该告警（不许误报）。"""
    client.put(
        "/api/daily",
        json={
            "shop_id": shop.id,
            "stat_date": "2026-03-02",
            "values": {"revenue": 5000},
            "shifts": [{"worker_name": "小时工甲", "start_time": "11:00", "end_time": "14:00"}],
        },
    )
    data = _report_range(client, shop.id, "2026-03-01", "2026-03-31")
    assert data["observations"]["input_alerts"] == []


# =========================================================================== #
# 16. 系统端点
# =========================================================================== #
def test_health_ok(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    # M3 授权后字段数 11 → 16（M3-2 新增 5 个隐性成本字段），仅更新期望值
    assert body["enabled_fields"] == 16
    assert body["enabled_metrics"] >= 20
    # LLM 抽象层状态可见，且不回显 key
    assert body["llm"]["enabled"] is False
    assert "api_key" not in body["llm"]
    assert body["llm"]["api_key_set"] in (True, False)


def test_swagger_available(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_meta_endpoints_expose_registries(client):
    # M3 授权后 field_def 11 → 16（M3-2 新增 5 个隐性成本字段），仅更新期望值
    fields = client.get("/api/meta/fields").json()
    assert {f["code"] for f in fields} == {
        # 施工单 §3.1 的 6 个原子数字
        "revenue",
        "purchase",
        "orders",
        "rent_m",
        "utility_m",
        "gross_margin",
        # M2-1 新增的 5 个全职人力字段
        "full_time_headcount",
        "full_time_monthly",
        "social_insurance_m",
        "housing_fund_m",
        "welfare_m",
        # M3-2 新增的 5 个隐性成本字段
        "staff_meal_m",
        "tableware_m",
        "repair_m",
        "cash_diff_m",
        "returned_m",
    }
    day_fields = client.get("/api/meta/fields", params={"scope": "day"}).json()
    assert {f["code"] for f in day_fields} == {"revenue", "purchase", "orders"}
    month_fields = client.get("/api/meta/fields", params={"scope": "month"}).json()
    assert len(month_fields) == 13     # 3 个原有 + 5 个 M2-1 + 5 个 M3-2

    metrics = client.get("/api/meta/metrics").json()
    codes = {m["code"] for m in metrics}
    assert {"labor_cost_rate", "efficiency", "avg_ticket", "food_cost_rate", "breakeven_daily"} <= codes
    # M2-1 新增的原子量必须进注册表，否则前端和引擎都看不到它
    assert "labor_m_full" in codes
    # M3 授权新增的两个派生指标
    assert {"labor_cost_rate_full", "stock_variance_rate"} <= codes


def test_shift_hours_boundaries():
    assert shift_hours("00:00", "23:59") == pytest.approx(23 + 59 / 60, abs=0.001)
    assert shift_hours("23:30", "00:30") == pytest.approx(1.0)
    assert shift_hours("12:00", "12:00") == pytest.approx(0.0)


def test_date_bounds_are_ok():
    assert PERIOD_START < PERIOD_END
    assert isinstance(date(2026, 10, 1), date)
