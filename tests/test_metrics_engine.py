"""MetricsEngine 单测（施工单 3.5 的四道守卫 + 验收硬门禁 #3）。

原则：**先红后绿**。下面每条守卫都有一个"这个断言本来会失败"的构造——
比如 test_divisor_metric_returns_none 用的是真会除零的数据，
test_cross_period_is_ratio_of_sums 用的两天数据会让"对比率取平均"得出 80 而不是 33.33。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import text

from conftest import PERIOD_END, PERIOD_START
from server.metrics_engine import (
    CyclicDependencyError,
    DerivedMetricAsDivisorError,
    FormulaParser,
    FormulaSyntaxError,
    MetricsEngine,
    RatioAsInputError,
    UnknownFieldError,
    shift_hours,
)
from server.models import FactDaily, MetricValue, Shift

pytestmark = pytest.mark.usefixtures("session")


# =========================================================================== #
# 1. 公式解析器
# =========================================================================== #
# =========================================================================== #
# 0. 架构范围守卫 —— 把蓝图索引 §3 的「范围警告」变成机器能拦的检查
#
# 这两条测试的存在理由：警告写在文档里拦不住人，写在测试里才拦得住。
# 它们会在"顺手加一张表"或"顺手加第 6 个指标"的那一刻变红，
# 逼着做的人回去确认范围（这是刻意的摩擦，不是测试写错了）。
# =========================================================================== #
def test_scope_exactly_eighteen_tables_with_locked_tables_absent():
    """M5 授权后：表集合必须**恰好 18 张**；仍锁定的表一张都不许出现。

    原名 `test_scope_only_seven_tables_and_no_m2_tables`（M1 时恰好 7 张）。
    期望值随四次授权更新，**断言结构一字未动**，也没有 skip：

    | 时点 | 授权 | 新增表 | 合计 |
    |---|---|---|---|
    | M1 | 施工单 §3.3 | shops/fact_daily/shifts/field_def/metric_def/metric_value/dim_calendar | 7 |
    | M3 | docs/03 §3 M3-3 | goods / stocktakes / scraps | 10 |
    | M4 | docs/04 §2 M4-3 | goods_categories | 11 |
    | M4 | docs/04 §3 M4-4 | purchases / receipts | 13 |
    | M4 | docs/04 §2bis.3 M4-12 | goods_aliases（别名表） | 14 |
    | M5 | docs/05 §3 M5-0 | bom / bom_variance | 16 |
    | M5 | docs/05 §4 M5-1 | suppliers / payables | **18** |

    ⚠️ **与任务书 §3 的字面数字不一致，请评审复核**
    §3 写「解锁 `bom`；表数守卫 14 → 15」，但同节锚点点名的是
    **「新增 `Bom` / `BomVariance`」两个模型类**；§4 又授权 `suppliers` + `payables`。
    所以按锚点算是 14 + 2 + 2 = **18**，不是 15。

    为什么 `bom_variance` 不能省：M5-0 的账面公式要用区间内的**累计理论领用**
    （`Σ sold_qty × std_qty_gross`），这个量必须能按日期区间聚合；
    而"卖了多少份、实际领了多少"没有别的落点（任务书没有授权领料单表）。
    **不落库就得每次重算且无法追溯**，差异分析也就没法按菜/按料排序。

    仍然锁定（出现即红）：`goods_version` / `stock_moves` / `recipes`。
    用 AST 取**字符串常量**来判，不用正则扫原文 —— 否则文档里提一句就会误报。
    """
    import ast
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    expected = {
        # M1 的 7 张
        "shops",
        "fact_daily",
        "shifts",
        "field_def",
        "metric_def",
        "metric_value",
        "dim_calendar",
        # M3-3 授权的 3 张
        "goods",
        "stocktakes",
        "scraps",
        # M4 授权的 4 张
        "goods_categories",
        "purchases",
        "receipts",
        "goods_aliases",
        # M5-0 授权的 2 张（docs/05 §3 锚点：Bom / BomVariance）
        "bom",
        "bom_variance",
        # M5-1 授权的 2 张（docs/05 §4）
        "suppliers",
        "payables",
    }
    models_src = (root / "server" / "models.py").read_text(encoding="utf-8")
    actual = set(re.findall(r'__tablename__\s*=\s*"([^"]+)"', models_src))
    assert actual == expected, f"表集合不对：多出 {sorted(actual - expected)}，缺少 {sorted(expected - actual)}"

    forbidden = {"goods_version", "stock_moves", "recipes"}
    offenders: list[str] = []
    for path in sorted((root / "server").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.strip().lower() in forbidden:
                    offenders.append(f"{path.name}:{node.lineno} \"{node.value}\"")
    assert not offenders, f"server/ 里出现了仍锁定的表名：{offenders}"


def test_scope_exactly_eight_derived_metrics():
    """M6 授权后：派生指标必须**恰好 8 个**。

    原名 `test_scope_only_seven_tables_and_no_m2_tables` 系列，几经改名。
    期望值随**三处授权**更新（**断言结构与判定逻辑一字未动**，没有 skip）：
      * M3-0（`docs/03` §2）授权第 6 个：`labor_cost_rate_full`
      * M3-3（`docs/03` §3）授权第 7 个：`stock_variance_rate`
      * **M6（`docs/06` §0 裁定 7）授权第 8 个：`explained_loss_rate`**
        （可解释损耗率 = 已登记且通过审核的损耗 ÷ 总损耗）
      * M2-1 授权的 5 个字段 → 再 +5 个隐性成本字段 = `field_def` 16 个
      * M3 新增的原子量 → M5-0 补 BOM 相关 → M6 补 `explained_loss_m` → 原子量 14 个
    """
    from server.seed_metrics import CORE_METRIC_CODES, FIELD_SEEDS, METRIC_SEEDS

    derived = [m["code"] for m in METRIC_SEEDS if m["layer"] == "derived"]
    atomic_metrics = [m["code"] for m in METRIC_SEEDS if m["layer"] == "atomic"]

    assert sorted(derived) == sorted(CORE_METRIC_CODES)
    assert len(derived) == 8, f"M6 授权后只允许 8 个派生指标，当前 {len(derived)}：{derived}"

    assert len(FIELD_SEEDS) == 16, "6 个原子数字 + 5 个全职人力字段 + 5 个隐性成本字段"
    assert sorted(atomic_metrics) == [
        "book_profit_m",
        "explained_loss_m",
        "hidden_cost_m",
        "hours",
        "labor_cost",
        "labor_m",
        "labor_m_full",
        "purchase_m",
        "real_profit_m",
        "revenue_m",
        "revenue_m_avg",
        "scrap_amount",
        "stock_actual_amount",
        "stock_book_amount",
        "stock_variance_amount",
    ]
    assert len(FIELD_SEEDS) + len(atomic_metrics) == 31

    # 每个派生指标都必须有大白话模板，否则报告会退化成术语
    for metric in METRIC_SEEDS:
        if metric["layer"] == "derived":
            assert metric["plain_text"], f"{metric['code']} 缺 plain_text"
            assert "{value}" in metric["plain_text"], f"{metric['code']} 的 plain_text 没有 {{value}} 占位"


# =========================================================================== #
# 7. M2-6 · 组合层输入约束（比率不许当公式输入）
# =========================================================================== #
def test_ratio_as_plain_input_is_rejected(session, shop):
    """两个比率相加（food_cost_rate + labor_cost_rate 这类）必须被引擎拒掉。

    它算出来会是一个"看着像数"的东西，但语义上毫无意义 —— 靠人自觉守不住。
    """
    _insert_metric(session, code="bad_ratio_sum", formula="food_cost_rate + 1")
    engine = MetricsEngine(session)
    with pytest.raises(RatioAsInputError) as exc:
        engine.validate()
    assert "aggregation=ratio" in str(exc.value)

    # 也必须是更宽的那个异常的子类关系正确（做分母是它的特例）
    assert issubclass(DerivedMetricAsDivisorError, RatioAsInputError)


def test_ratio_as_divisor_raises_both_names(session, shop):
    """比率做分母：既按具体的 DerivedMetricAsDivisorError 能抓到，也按 RatioAsInputError 能抓到。"""
    _insert_metric(session, code="bad_ratio_divisor", formula="revenue / food_cost_rate")
    engine = MetricsEngine(session)
    with pytest.raises(DerivedMetricAsDivisorError):
        engine.validate()
    with pytest.raises(RatioAsInputError):
        engine.validate()


def test_additive_inputs_are_allowed(session, shop):
    """放行侧（docs/02 §4 M2-6 验收表）：hours 是 additive、labor_m_full 是 additive 相加。

    这条是 M2-6 的边界 —— 判定依据必须是 `aggregation`，**不是**"是否派生"。
    `efficiency = revenue / hours` 里的 hours 也是派生出来的，按"派生就拒"会把它判死。
    """
    engine = MetricsEngine(session)
    engine.validate()   # efficiency（除 hours）、labor_m_full（additive 相加）都在里面，必须放行

    assert engine.is_ratio_aggregated(engine.metric_defs["food_cost_rate"]) is True
    assert engine.is_ratio_aggregated(engine.metric_defs["hours"]) is False
    assert engine.is_ratio_aggregated(engine.metric_defs["labor_m_full"]) is False

    # 真值也验一遍：labor_m = 200，加 4 个字段后 = 26400
    #
    # 注意这 5 个新字段**不是 fact_daily 的类型化列**，它们落在 extra_json 溢出通道里
    # （蓝图 v1.0 §2.2 纪律二：宽表 + JSON 扩展列，表结构永不变）。
    # 所以必须走产品写入路径，不能直接 FactDaily(**kwargs)。
    from server.routers.daily import upsert_fact

    upsert_fact(session, shop.id, date(2026, 10, 1), "month",
                {"rent_m": 6000.0, "utility_m": 1500.0, "gross_margin": 0.6})
    upsert_fact(session, shop.id, date(2026, 10, 1), "month",
                {"full_time_monthly": 20000.0, "social_insurance_m": 4800.0,
                 "housing_fund_m": 1000.0, "welfare_m": 400.0})
    session.add(Shift(shop_id=shop.id, work_date=date(2026, 10, 5), worker_name="全职A",
                      start_time="09:00", end_time="17:00", hours=8.0, hourly_rate=25.0))
    session.commit()
    values = MetricsEngine(session).compute(shop.id, date(2026, 10, 1), date(2026, 10, 31))
    assert values["labor_m"] == pytest.approx(200.0)
    assert values["labor_m_full"] == pytest.approx(26400.0)


def test_formula_function_names_not_treated_as_variables():
    """函数名不是变量。

    回归守卫：`names()` 曾经把 `abs(x)` 里的 `abs` 当成变量收进依赖集，
    于是任何用白名单函数的公式都会被判"引用了不存在的字段：abs"。
    M1 的白名单支持 min/max/abs/round，但**没有任何种子指标用到**，
    所以这个坑一直没被踩到 —— 直到 M3-3 第一次写出 `abs(stock_variance_amount)`。
    """
    tree = FormulaParser.parse("abs(a) / max(b, c) + round(d, 1)")
    assert FormulaParser.names(tree) == {"a", "b", "c", "d"}
    assert FormulaParser.divisor_names(tree) == {"b", "c"}
    # 求值仍然正常：|−4| / max(8,2) + round(1.234,1) = 0.5 + 1.2
    assert FormulaParser.evaluate(tree, {"a": -4.0, "b": 8.0, "c": 2.0, "d": 1.234}) == pytest.approx(1.7)


def test_parse_and_dependencies():
    tree = FormulaParser.parse("(rent_m + utility_m + labor_m) / 30 / gross_margin")
    assert FormulaParser.names(tree) == {
        "rent_m",
        "utility_m",
        "labor_m",
        "gross_margin",
    }
    # 两个除法：分母分别是 30（常量）和 gross_margin，所以只有 gross_margin 是变量分母
    assert FormulaParser.divisor_names(tree) == {"gross_margin"}


def test_divisor_names_covers_nested_and_function_args():
    assert FormulaParser.divisor_names(FormulaParser.parse("a / (b * c)")) == {"b", "c"}
    assert FormulaParser.divisor_names(FormulaParser.parse("min(x, a / b)")) == {"b"}
    assert FormulaParser.divisor_names(FormulaParser.parse("a * b")) == set()


@pytest.mark.parametrize(
    "expr",
    [
        "revenue.__class__",          # 属性访问
        "revenue[0]",                 # 下标
        "len(revenue)",               # 白名单外函数
        "revenue ** 2",               # 幂运算
        "__import__('os').system('x')",
        "[x for x in range(3)]",
        "lambda: 1",
        "revenue if revenue else 0",  # 三元表达式
        "'abc'",                      # 字符串常量
    ],
)
def test_formula_syntax_rejects_unsafe(expr: str):
    with pytest.raises(FormulaSyntaxError):
        FormulaParser.parse(expr)


def test_formula_allows_safe_functions():
    tree = FormulaParser.parse("max(revenue / orders, 1) + round(1.234, 1)")
    # max(400/8, 1) = 50.0，round(1.234, 1) = 1.2
    assert FormulaParser.evaluate(tree, {"revenue": 400.0, "orders": 8.0}) == pytest.approx(51.2)


# =========================================================================== #
# 2. 守卫 1：分母为 0 或缺失 → None（不报错、不算成 0）
# =========================================================================== #
def test_divisor_zero_returns_none(session, shop):
    session.add(FactDaily(shop_id=shop.id, stat_date=date(2026, 10, 1), period_type="day",
                          revenue=0.0, purchase=100.0, orders=0.0))
    session.commit()
    engine = MetricsEngine(session)
    values = engine.compute(shop.id, date(2026, 10, 1), date(2026, 10, 1))

    assert values["revenue"] == 0.0          # 0 是真实值，要留住
    assert values["food_cost_rate"] is None  # 0 不能当分母
    assert values["avg_ticket"] is None
    assert values["labor_cost_rate"] is None


def test_missing_field_returns_none_not_zero(session, shop):
    """营业额未填：只算工时，人力成本率返回 None（施工单 3.6）。"""
    session.add(Shift(shop_id=shop.id, work_date=date(2026, 10, 2), worker_name="员工A",
                      start_time="18:00", end_time="22:00", hours=4.0, hourly_rate=25.0))
    session.commit()
    engine = MetricsEngine(session)
    values = engine.compute(shop.id, date(2026, 10, 2), date(2026, 10, 2))

    assert values["hours"] == 4.0
    assert values["labor_cost"] == 100.0
    assert values["labor_cost_rate"] is None
    assert values["efficiency"] is None
    assert values["revenue"] is None


def test_none_propagates_through_addition(session, shop):
    """月房租缺失 → 保本营业额是 None，不是把缺失当 0 算出一个假数。"""
    session.add(FactDaily(shop_id=shop.id, stat_date=date(2026, 10, 1), period_type="month",
                          rent_m=None, utility_m=1500.0, gross_margin=0.6))
    session.add(Shift(shop_id=shop.id, work_date=date(2026, 10, 3), worker_name="员工A",
                      start_time="18:00", end_time="22:00", hours=4.0, hourly_rate=25.0))
    session.commit()
    engine = MetricsEngine(session)
    values = engine.compute(shop.id, date(2026, 10, 1), date(2026, 10, 3))
    assert values["rent_m"] is None
    assert values["breakeven_daily"] is None


def test_default_value_applies_only_to_fields_that_have_one(session, shop):
    """field_def.default_value 的语义：字段缺失时用它兜底。

    特意只给 gross_margin 兜底值（0.6，老板不知道毛利率时的估法）。
    rent_m / utility_m **故意没有兜底值** —— 不知道房租就不该编一个自信的保本数字。
    """
    session.add(Shift(shop_id=shop.id, work_date=date(2026, 10, 3), worker_name="员工A",
                      start_time="18:00", end_time="22:00", hours=4.0, hourly_rate=25.0))
    session.commit()
    engine = MetricsEngine(session)
    values = engine.compute(shop.id, date(2026, 10, 1), date(2026, 10, 3))

    assert values["gross_margin"] == pytest.approx(0.6)   # 有兜底值
    assert values["rent_m"] is None                       # 无兜底值 → 缺失
    assert values["utility_m"] is None
    assert values["breakeven_daily"] is None              # 所以保本算不出，而不是算出一个假数

    blocked = {item["code"]: item["missing"] for item in engine.blocked_reasons(values)}
    assert "breakeven_daily" in blocked
    assert "月房租" in blocked["breakeven_daily"]
    assert "月水电" in blocked["breakeven_daily"]


# =========================================================================== #
# 3. 守卫 2 / 3：派生比率不许做分母、未知字段必须炸
# =========================================================================== #
def _insert_metric(session, **overrides) -> None:
    row = {
        "code": "tmp_metric",
        "name": "临时指标",
        "layer": "derived",
        "unit": "元",
        "formula": "revenue / orders",
        "depends_on": None,
        "benchmark_key": None,
        "warn_high": None,
        "warn_low": None,
        "higher_better": None,
        "plain_text": None,
        "enabled": 1,
        "version": 1,
        "aggregation": "ratio",
        "scope": "day",
        "inputs": None,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join(f":{k}" for k in row)
    session.execute(text(f"INSERT INTO metric_def ({cols}) VALUES ({marks})"), row)
    session.commit()


def test_unknown_field_raises(session, shop):
    _insert_metric(session, code="bad_unknown", formula="revenue / non_existent_field")
    engine = MetricsEngine(session)
    with pytest.raises(UnknownFieldError) as exc:
        engine.validate()
    assert "non_existent_field" in str(exc.value)


def test_derived_rate_as_divisor_raises(session, shop):
    """food_cost_rate 是比率（不可加），拿它做分母必须被拦。

    这条守卫的用意：比率跨期既不能相加也不能取平均，
    一旦进了分母，7 天区间算出来的数就是错的，而且是"看着像真的"的错。
    """
    _insert_metric(session, code="bad_divisor", formula="revenue / food_cost_rate")
    engine = MetricsEngine(session)
    with pytest.raises(DerivedMetricAsDivisorError) as exc:
        engine.validate()
    assert "food_cost_rate" in str(exc.value)


def test_additive_derived_as_divisor_is_allowed(session, shop):
    """hours / labor_cost 是可加派生量，做分母合法 —— 否则施工单自己的
    efficiency = revenue / hours 就会被判死。这一条是守卫 2 的边界。"""
    _insert_metric(session, code="ok_divisor", formula="revenue / hours")
    engine = MetricsEngine(session)
    engine.validate()  # 不应抛异常

    session.add(FactDaily(shop_id=shop.id, stat_date=date(2026, 10, 1), period_type="day",
                          revenue=400.0))
    session.add(Shift(shop_id=shop.id, work_date=date(2026, 10, 1), worker_name="员工A",
                      start_time="18:00", end_time="22:00", hours=4.0, hourly_rate=25.0))
    session.commit()
    values = MetricsEngine(session).compute(shop.id, date(2026, 10, 1), date(2026, 10, 1))
    assert values["ok_divisor"] == pytest.approx(100.0)


def test_topological_order_respects_dependencies(session, shop):
    """拓扑序必须真的排过 —— 不能只是"恰好没报错"。

    这条是回归守卫：曾经 dependency_graph 里写成边建边查，依赖被整片吞掉，
    拓扑序退化成字母序，但所有数值测试照样通过（因为顺序碰巧也对）。
    """
    engine = MetricsEngine(session)
    order = engine.topo_order()
    index = {code: i for i, code in enumerate(order)}

    assert index["labor_m"] < index["labor_m_full"]
    assert index["labor_m_full"] < index["breakeven_daily"]
    assert index["hours"] < index["efficiency"]
    assert index["labor_cost"] < index["labor_cost_rate"]

    graph = engine.dependency_graph()
    # M2-1 授权后 breakeven_daily 的人工口径换成 labor_m_full（docs/02 §2 M2-1）
    assert graph["breakeven_daily"] == {"labor_m_full"}
    assert graph["labor_m_full"] == {"labor_m"}
    assert graph["efficiency"] == {"hours"}
    assert graph["labor_cost_rate"] == {"labor_cost"}
    assert graph["avg_ticket"] == set()   # 只依赖字段，不依赖指标


def test_cyclic_dependency_raises(session, shop):
    _insert_metric(session, code="cyc_a", formula="cyc_b + 1")
    _insert_metric(session, code="cyc_b", formula="cyc_a + 1")
    engine = MetricsEngine(session)
    with pytest.raises(CyclicDependencyError):
        engine.topo_order()


def test_atomic_metric_without_source_raises(session, shop):
    _insert_metric(session, code="ghost_atomic", layer="atomic", formula=None)
    engine = MetricsEngine(session)
    with pytest.raises(UnknownFieldError):
        engine.validate()


# =========================================================================== #
# 4. 守卫 4：跨期聚合 —— 先 Σ 原子再算比率
# =========================================================================== #
def test_cross_period_is_ratio_of_sums_not_mean_of_ratios(session, shop):
    """两天：100元/10单(=10) 与 300元/2单(=150)。

    正确：400 / 12 = 33.33    ← 先加原子
    错误：mean(10, 150) = 80  ← 对比率取平均
    两条断言一起下，才能证明这里真的走了正确那条路。
    """
    session.add_all(
        [
            FactDaily(shop_id=shop.id, stat_date=date(2026, 10, 1), period_type="day",
                      revenue=100.0, purchase=20.0, orders=10.0),
            FactDaily(shop_id=shop.id, stat_date=date(2026, 10, 2), period_type="day",
                      revenue=300.0, purchase=60.0, orders=2.0),
        ]
    )
    session.commit()
    engine = MetricsEngine(session)
    values = engine.compute(shop.id, date(2026, 10, 1), date(2026, 10, 2))

    assert values["revenue"] == pytest.approx(400.0)      # SUM 而不是 AVG
    assert values["orders"] == pytest.approx(12.0)
    assert values["avg_ticket"] == pytest.approx(400 / 12, abs=0.01)
    assert abs(values["avg_ticket"] - 80.0) > 1.0         # 明确排除"取平均"那条错误路径


def test_hours_sum_across_period(session, shop):
    for day, hours in ((1, 4.0), (2, 6.0), (3, 8.0)):
        session.add(Shift(shop_id=shop.id, work_date=date(2026, 10, day), worker_name="员工A",
                          start_time="18:00", end_time="22:00", hours=hours, hourly_rate=25.0))
    session.commit()
    values = MetricsEngine(session).compute(shop.id, date(2026, 10, 1), date(2026, 10, 3))
    assert values["hours"] == pytest.approx(18.0)
    assert values["labor_cost"] == pytest.approx(450.0)


def test_month_scope_atom_uses_anchor_month_not_period_sum(session, shop):
    """月口径原子量取"区间结束日所在月"，不随区间天数变化。"""
    session.add(FactDaily(shop_id=shop.id, stat_date=date(2026, 10, 1), period_type="month",
                          rent_m=6000.0, utility_m=1500.0, gross_margin=0.6))
    session.add(FactDaily(shop_id=shop.id, stat_date=date(2026, 11, 1), period_type="month",
                          rent_m=9000.0, utility_m=0.0, gross_margin=0.6))
    session.commit()
    engine = MetricsEngine(session)

    october = engine.compute(shop.id, date(2026, 10, 1), date(2026, 10, 7))
    november = engine.compute(shop.id, date(2026, 11, 1), date(2026, 11, 7))
    assert october["rent_m"] == pytest.approx(6000.0)
    assert november["rent_m"] == pytest.approx(9000.0)


# =========================================================================== #
# 5. 落库与幂等
# =========================================================================== #
def test_metric_value_cache_written_and_idempotent(session, sample_shop):
    engine = MetricsEngine(session)
    engine.compute(sample_shop.id, PERIOD_START, PERIOD_END)
    session.commit()

    first = session.query(MetricValue).filter_by(shop_id=sample_shop.id).count()
    assert first > 0

    engine.compute(sample_shop.id, PERIOD_START, PERIOD_END)
    session.commit()
    second = session.query(MetricValue).filter_by(shop_id=sample_shop.id).count()
    assert first == second, "同一区间重复计算不应产生重复行（主键是 shop+period+metric）"


def test_shift_hours_helper():
    assert shift_hours("18:00", "22:00") == pytest.approx(4.0)
    assert shift_hours("22:00", "06:00") == pytest.approx(8.0)   # 跨夜
    assert shift_hours("18:00", "18:00") == pytest.approx(0.0)
    assert shift_hours("09:30", "14:00") == pytest.approx(4.5)


# =========================================================================== #
# 6. ⭐ 验收硬门禁 #3：加一个新指标 = 只插 metric_def 一行
# =========================================================================== #
def test_gate3_new_metric_by_single_row_insert(session, sample_shop):
    """**做不到即架构失败**（施工单 §4 门禁 3）。

    这里模拟的是"开发者打开 SQLite 客户端，往 metric_def 插一行"这个动作：
    - 不改 Python
    - 不改前端
    - 不重启进程之外的重建

    然后要求：引擎能算它、报告能带它、前端 meta 接口能拿到它。
    """
    from server.routers.report import assemble_report

    before = MetricsEngine(session).compute(sample_shop.id, PERIOD_START, PERIOD_END)
    assert "dine_in_avg_ticket" not in before

    # ---- 唯一的动作：插一行 ----
    _insert_metric(
        session,
        code="dine_in_avg_ticket",
        name="堂食客单价",
        formula="revenue / orders",
        depends_on="revenue,orders",
        plain_text="平均每单 {value} 元",
        aggregation="ratio",
        scope="day",
    )

    # ---- 之后不做任何代码改动 ----
    engine = MetricsEngine(session)  # 新引擎实例 = 重新读表，等价于重启服务
    values = engine.compute(sample_shop.id, PERIOD_START, PERIOD_END)
    assert "dine_in_avg_ticket" in values
    assert values["dine_in_avg_ticket"] == pytest.approx(23000 / 755, abs=0.01)

    report = assemble_report(session, sample_shop.id, PERIOD_START, PERIOD_END)
    codes = {m.code for m in report.metrics}
    assert "dine_in_avg_ticket" in codes
    new_metric = next(m for m in report.metrics if m.code == "dine_in_avg_ticket")
    assert new_metric.sentence == "平均每单 30.46 元"
    assert new_metric.display == "30.46"


def test_gate3_via_http_meta_endpoint(client, session, sample_shop):
    """同一件事再从 HTTP 走一遍：前端拿 meta 就能渲染出来，无需发版。"""
    _insert_metric(session, code="weekend_ticket", name="周末客单价", formula="revenue / orders")
    res = client.get("/api/meta/metrics")
    assert res.status_code == 200
    codes = {m["code"] for m in res.json()}
    assert "weekend_ticket" in codes

    res = client.get(
        "/api/report",
        params={
            "shop_id": sample_shop.id,
            "start": PERIOD_START.isoformat(),
            "end": PERIOD_END.isoformat(),
        },
    )
    assert res.status_code == 200
    assert "weekend_ticket" in {m["code"] for m in res.json()["metrics"]}
