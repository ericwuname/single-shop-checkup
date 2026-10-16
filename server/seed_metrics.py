"""种子数据（施工单 3.4 / server/seed_metrics.py）。

把「有哪些字段、有哪些指标、长什么样、怎么算」全部灌进三张元数据表。
**这是本项目的架构地基**：验收硬门禁 #3 要求"加一个新指标只插一行"，靠的就是这里。

幂等：重复跑只更新定义，不重复插入；定义真变了才 ``version += 1``；
``enabled`` 是运行时开关，种子不覆盖它（防止把用户手动关掉的指标又打开）。
"""

from __future__ import annotations

import calendar as _calendar
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .metrics_engine import FormulaParser
from .models import DimCalendar, FieldDef, MetricDef

# =========================================================================== #
# 6 个原子字段（施工单 3.1「6 个原子数字录入」）
#   日口径 3 个：营业额 / 买菜 / 订单数   ← 老板每天微信发的就是这几个
#   月口径 3 个：月房租 / 月水电 / 毛利率 ← 谈店时问一次，之后不改
# =========================================================================== #
FIELD_SEEDS: list[dict[str, Any]] = [
    {
        "code": "revenue",
        "entity": "fact_daily",
        "label": "营业额",
        "data_type": "number",
        "unit": "元",
        "scope": "day",
        "is_required": 0,
        "default_value": None,
        "enum_values": None,
        "sort_order": 1,
        "help_text": "今天卖了多少。有零有整照实写，估的也先写上。",
    },
    {
        "code": "purchase",
        "entity": "fact_daily",
        "label": "买菜进货",
        "data_type": "number",
        "unit": "元",
        "scope": "day",
        "is_required": 0,
        "default_value": None,
        "enum_values": None,
        "sort_order": 2,
        "help_text": "今天买菜、买肉、买调料、买包装花了多少。",
    },
    {
        "code": "orders",
        "entity": "fact_daily",
        "label": "订单数",
        "data_type": "integer",
        "unit": "单",
        "scope": "day",
        "is_required": 0,
        "default_value": None,
        "enum_values": None,
        "sort_order": 3,
        "help_text": "今天一共来了多少单（一桌/一单/一客都算一单，怎么数固定一种就行）。",
    },
    {
        "code": "rent_m",
        "entity": "fact_daily",
        "label": "月房租",
        "data_type": "number",
        "unit": "元",
        "scope": "month",
        "is_required": 0,
        # 故意不给默认值：不知道房租就不知道该做多少才不亏，
        # 这时候必须返回 None 并提示"缺月房租"，不能默认成 0 编一个自信的错数。
        "default_value": None,
        "enum_values": None,
        "sort_order": 11,
        "help_text": "一个月房租多少（含物业就一起写）。",
    },
    {
        "code": "utility_m",
        "entity": "fact_daily",
        "label": "月水电燃气",
        "data_type": "number",
        "unit": "元",
        "scope": "month",
        "is_required": 0,
        "default_value": None,
        "enum_values": None,
        "sort_order": 12,
        "help_text": "一个月水电气加一起多少，估一个也行。",
    },
    {
        "code": "gross_margin",
        "entity": "fact_daily",
        "label": "毛利率",
        "data_type": "ratio",
        "unit": "比例",
        "scope": "month",
        "is_required": 0,
        "default_value": "0.6",
        "enum_values": None,
        "sort_order": 13,
        "help_text": "卖 100 块钱的菜，刨掉食材还剩多少钱。不确定就留 0.6。",
    },
    # ------------------------------------------------------------------ #
    # M2-1 新增 5 个字段（docs/02 §2 M2-1）—— 零 DDL，只插 field_def 行
    #
    # 为什么必须给 default_value="0"：
    #   04 号判据要求"某月只填了 rent_m、没填全职月薪时，labor_m_full 必须退化为 labor_m，
    #   不许返回 None 或 0 导致保本线变低"。formula 里任一项为 None 会让整个加法返回 None，
    #   保本营业额跟着变 None。给 0 兜底 → 加法退化成 labor_m + 0 + 0 + 0 + 0 = labor_m。
    # ------------------------------------------------------------------ #
    {
        "code": "full_time_headcount",
        "entity": "fact_daily",
        "label": "全职人数",
        "data_type": "integer",
        "unit": "人",
        "scope": "month",
        "is_required": 0,
        "default_value": "0",
        "enum_values": None,
        "sort_order": 14,
        "help_text": "店里拿月薪的有几个人。",
    },
    {
        "code": "full_time_monthly",
        "entity": "fact_daily",
        "label": "全职月薪合计",
        "data_type": "number",
        "unit": "元",
        "scope": "month",
        "is_required": 0,
        "default_value": "0",
        "enum_values": None,
        "sort_order": 15,
        "help_text": "拿月薪的这几个人，一个月工资加起来多少（不含社保）。",
    },
    {
        "code": "social_insurance_m",
        "entity": "fact_daily",
        "label": "五险",
        "data_type": "number",
        "unit": "元",
        "scope": "month",
        "is_required": 0,
        "default_value": "0",
        "enum_values": None,
        "sort_order": 16,
        "help_text": "一个月给员工交的社保（单位那部分）。",
    },
    {
        "code": "housing_fund_m",
        "entity": "fact_daily",
        "label": "公积金",
        "data_type": "number",
        "unit": "元",
        "scope": "month",
        "is_required": 0,
        "default_value": "0",
        "enum_values": None,
        "sort_order": 17,
        "help_text": "一个月交的公积金（单位那部分）。没交就留 0。",
    },
    {
        "code": "welfare_m",
        "entity": "fact_daily",
        "label": "福利与员工餐",
        "data_type": "number",
        "unit": "元",
        "scope": "month",
        "is_required": 0,
        "default_value": "0",
        "enum_values": None,
        "sort_order": 18,
        "help_text": "员工餐、节假日加餐、发的东西，一个月大概多少。",
    },
    # ------------------------------------------------------------------ #
    # M3-2 新增 5 个字段（docs/03 §3 M3-2）—— 隐性成本，月口径，零 DDL
    #
    # 这五项的共同点：老板**本来就知道**这些数，只是从来没把它们算进"我赚了多少"。
    # 所以这个任务不需要老板新增任何动作（符合 P1：每日录入不得增加 30 秒）。
    # 同样给 default_value="0" 兜底，missing 时不返回 None，避免利润类指标整体变 None。
    # ------------------------------------------------------------------ #
    {
        "code": "staff_meal_m",
        "entity": "fact_daily",
        "label": "员工餐",
        "data_type": "number",
        "unit": "元",
        "scope": "month",
        "is_required": 0,
        "default_value": "0",
        "enum_values": None,
        "sort_order": 21,
        "help_text": "管员工几顿饭。按人数 × 15 元/天 × 营业天数估（年约 2–3 万）。",
    },
    {
        "code": "tableware_m",
        "entity": "fact_daily",
        "label": "餐具破损与更换",
        "data_type": "number",
        "unit": "元",
        "scope": "month",
        "is_required": 0,
        "default_value": "0",
        "enum_values": None,
        "sort_order": 22,
        "help_text": "碗碟、打包盒、筷子这些一个季度买了多少，摊到每月（年约 0.6–1.2 万）。",
    },
    {
        "code": "repair_m",
        "entity": "fact_daily",
        "label": "设备维修",
        "data_type": "number",
        "unit": "元",
        "scope": "month",
        "is_required": 0,
        "default_value": "0",
        "enum_values": None,
        "sort_order": 23,
        "help_text": "冰箱、灶、和面机修过几次，一个月大概多少（年约 0.5–2 万）。",
    },
    {
        "code": "cash_diff_m",
        "entity": "fact_daily",
        "label": "收银差异/漏单",
        "data_type": "number",
        "unit": "元",
        "scope": "month",
        "is_required": 0,
        "default_value": "0",
        "enum_values": None,
        "sort_order": 24,
        "help_text": "盘账时发现的短款或漏单。经验值：流水的 0.3%–1%。",
    },
    {
        "code": "returned_m",
        "entity": "fact_daily",
        "label": "退菜报废",
        "data_type": "number",
        "unit": "元",
        "scope": "month",
        "is_required": 0,
        "default_value": "0",
        "enum_values": None,
        "sort_order": 25,
        "help_text": "退掉的菜、倒掉的料，一个月大概多少钱。",
    },
]

# =========================================================================== #
# 指标种子（施工单 3.4 的 5 个指标 + 3 个支撑原子量）
#
# layer = atomic   → 值直接可得（来自 field_def 或引擎注入）
# layer = derived  → 值由 formula 算
# aggregation      → additive: 跨期可加；ratio: 比率，跨期只能"先加原子再算"
# =========================================================================== #
METRIC_SEEDS: list[dict[str, Any]] = [
    # ---- 支撑用的原子量（不出现在"核心 5 指标"里，但报告要用）----
    {
        "code": "hours",
        "name": "总工时",
        "layer": "atomic",
        "unit": "小时",
        "formula": None,
        "depends_on": None,
        "inputs": None,
        "aggregation": "additive",
        "scope": "day",
        "benchmark_key": None,
        "warn_high": None,
        "warn_low": None,
        "higher_better": None,
        "plain_text": "一共上了 {value} 小时",
    },
    {
        "code": "labor_cost",
        "name": "人力成本",
        "layer": "atomic",
        "unit": "元",
        "formula": None,
        "depends_on": None,
        "inputs": None,
        "aggregation": "additive",
        "scope": "day",
        "benchmark_key": None,
        "warn_high": None,
        "warn_low": None,
        "higher_better": None,
        "plain_text": "人工花了 {value} 元",
    },
    {
        "code": "labor_m",
        "name": "月人工合计",
        "layer": "atomic",
        "unit": "元",
        "formula": None,
        "depends_on": None,
        "inputs": None,
        "aggregation": "additive",
        "scope": "month",
        "benchmark_key": None,
        "warn_high": None,
        "warn_low": None,
        "higher_better": None,
        "plain_text": "这个月人工合计 {value} 元",
    },
    # ---- M2-1 新增：全口径月人工（docs/02 §2 M2-1）----
    # 注意它**带 formula 但 layer=atomic**：
    #   layer 表达"这个量的性质"（原子级、可跨期相加），formula 表达"值怎么来的"。
    #   全部输入都是 additive，相加仍是 additive —— 所以 M2-6 的组合层输入约束放行它。
    {
        "code": "labor_m_full",
        "name": "全口径月人工",
        "layer": "atomic",
        "unit": "元",
        "formula": "labor_m + full_time_monthly + social_insurance_m + housing_fund_m + welfare_m",
        "depends_on": None,          # 由 _fill_depends_on 自动补齐
        "inputs": None,
        "aggregation": "additive",
        "scope": "month",
        "benchmark_key": None,
        "warn_high": None,
        "warn_low": None,
        "higher_better": None,
        "plain_text": "全口径月人工 {value} 元",
    },
    # ---- M3-0：锚点月的营业额（日口径字段在月尺度上的聚合）----
    # 为什么需要它：labor_m_full 是月口径、revenue 是日口径，直接相除会错。
    # 口径解法见 labor_cost_rate_full 的注释。
    {
        "code": "revenue_m",
        "name": "月营业额",
        "layer": "atomic",
        "unit": "元",
        "formula": None,          # 引擎注入（锚点月的 Σrevenue）
        "depends_on": None,
        "inputs": None,
        "aggregation": "additive",
        "scope": "month",
        "benchmark_key": None,
        "warn_high": None,
        "warn_low": None,
        "higher_better": None,
        "plain_text": "这个月营业额 {value} 元",
    },
    {
        "code": "purchase_m",
        "name": "月买菜进货",
        "layer": "atomic",
        "unit": "元",
        "formula": None,
        "depends_on": None,
        "inputs": None,
        "aggregation": "additive",
        "scope": "month",
        "benchmark_key": None,
        "warn_high": None,
        "warn_low": None,
        "higher_better": None,
        "plain_text": "这个月买菜花了 {value} 元",
    },
    {
        "code": "revenue_m_avg",
        "name": "月化营业额（按区间日均折算）",
        "layer": "atomic",
        "unit": "元",
        "formula": None,          # 引擎注入：区间日均营业额 × 30
        "depends_on": None,
        "inputs": None,
        # 是**金额**不是率，所以 additive —— 分母位置放金额名正言顺
        "aggregation": "additive",
        "scope": "month",
        "benchmark_key": None,
        "warn_high": None,
        "warn_low": None,
        "higher_better": None,
        "plain_text": "按这个区间日均折算，一个月营业额是 {value} 元",
    },
    # ---- M3-3：账实差异的金额口径（区间口径，跟查询区间走）----
    {
        "code": "stock_book_amount",
        "name": "账面库存金额",
        "layer": "atomic",
        "unit": "元",
        "formula": None,
        "depends_on": None,
        "inputs": None,
        "aggregation": "additive",
        "scope": "period",
        "benchmark_key": None,
        "warn_high": None,
        "warn_low": None,
        "higher_better": None,
        "plain_text": "账面库存 {value} 元",
    },
    {
        "code": "stock_actual_amount",
        "name": "实盘库存金额",
        "layer": "atomic",
        "unit": "元",
        "formula": None,
        "depends_on": None,
        "inputs": None,
        "aggregation": "additive",
        "scope": "period",
        "benchmark_key": None,
        "warn_high": None,
        "warn_low": None,
        "higher_better": None,
        "plain_text": "实盘库存 {value} 元",
    },
    {
        "code": "scrap_amount",
        "name": "报损金额",
        "layer": "atomic",
        "unit": "元",
        "formula": None,
        "depends_on": None,
        "inputs": None,
        "aggregation": "additive",
        "scope": "period",
        "benchmark_key": None,
        "warn_high": None,
        "warn_low": None,
        "higher_better": None,
        "plain_text": "报损 {value} 元",
    },
    # ---- M6（docs/06 §0 裁定 7）：已登记且通过审核的损耗 ----
    # 行业指标「可解释损耗率 = 已登记且通过审核的损耗金额 ÷ 总损耗金额」。
    # 这正是 T01–T08 代码化换来的东西：老板看到的不再是干巴巴一个"损耗 8000"，
    # 而是"6500 有原因、1500 说不清"—— **能追责的才是能改进的**。
    # 判定口径：有 reason_code 即"已登记"；该笔触发过审批阈值的还必须带审批人
    # （没签字的损耗不算"已解释"，否则自填自审就失去意义）。
    {
        "code": "explained_loss_m",
        "name": "已解释损耗金额",
        "layer": "atomic",
        "unit": "元",
        "formula": None,
        "depends_on": None,
        "inputs": None,
        "aggregation": "additive",
        "scope": "period",
        "benchmark_key": None,
        "warn_high": None,
        "warn_low": None,
        "higher_better": None,
        "plain_text": "其中 {value} 元有登记原因",
    },
    # ---- M3-2：隐性成本合计（additive 相加，仍是 additive）----
    {
        "code": "hidden_cost_m",
        "name": "隐性成本合计",
        "layer": "atomic",
        "unit": "元",
        "formula": "staff_meal_m + tableware_m + repair_m + cash_diff_m + returned_m",
        "depends_on": None,
        "inputs": None,
        "aggregation": "additive",
        "scope": "month",
        "benchmark_key": None,
        "warn_high": None,
        "warn_low": None,
        "higher_better": None,
        "plain_text": "容易被漏掉的成本有 {value} 元",
    },
    # ---- M3-2：老板以为的利润（营业额 − 看得见的支出）----
    {
        "code": "book_profit_m",
        "name": "账面利润（老板以为的）",
        "layer": "atomic",
        "unit": "元",
        "formula": "revenue_m - purchase_m - labor_m_full - rent_m - utility_m",
        "depends_on": None,
        "inputs": None,
        "aggregation": "additive",
        "scope": "month",
        "benchmark_key": None,
        "warn_high": None,
        "warn_low": None,
        "higher_better": None,
        "plain_text": "你以为这个月赚了 {value} 元",
    },
    # ---- M3-2：扣掉隐性成本之后，实际剩多少 ----
    # M4-1 改名（docs/04 §1 裁定 6-4）：`real_profit_gap` → `real_profit_m`。
    # 旧名字里的 gap 装的其实是"实际利润"（差额是另一个数 = hidden_cost_m），容易误读。
    # 旧 code 进 RETIRED_METRIC_CODES，停用不删除。
    {
        "code": "real_profit_m",
        "name": "实际利润（扣掉隐性成本）",
        "layer": "atomic",
        "unit": "元",
        "formula": "book_profit_m - hidden_cost_m",
        "depends_on": None,
        "inputs": None,
        "aggregation": "additive",
        "scope": "month",
        "benchmark_key": None,
        "warn_high": None,
        "warn_low": None,
        "higher_better": None,
        "plain_text": "算上那些容易被漏掉的，实际是 {value} 元",
    },
    # ---- M3-3：差异金额（实盘 − 账面）----
    {
        "code": "stock_variance_amount",
        "name": "账实差异金额",
        "layer": "atomic",
        "unit": "元",
        "formula": "stock_actual_amount - stock_book_amount",
        "depends_on": None,
        "inputs": None,
        "aggregation": "additive",
        "scope": "period",
        "benchmark_key": None,
        "warn_high": None,
        "warn_low": None,
        "higher_better": None,
        "plain_text": "账实差异 {value} 元",
    },
    # ---- 施工单 3.4 的 5 个核心指标 ----
    {
        "code": "labor_cost_rate",
        "name": "人力成本率",
        "layer": "derived",
        "unit": "ratio",
        "formula": "labor_cost / revenue",
        "depends_on": "labor_cost,revenue",
        "inputs": "revenue",
        "aggregation": "ratio",
        "scope": "day",
        # M4-0（docs/04 §1 裁定 6-3）：**不给判断**。
        # 口径错配的后果是误导：只走"全职月薪"约定的店里，排班工钱率会掉到个位数百分比，
        # 拿 15%–20% 的区间去比就会报"人工占比偏低、人手可能不够"——那家店可能有 4 个全职。
        # 判断统一走 `labor_cost_rate_full`（全口径），本指标只作为"排班工钱"的原始口径展示。
        "benchmark_key": None,
        "warn_high": None,
        "warn_low": None,
        "higher_better": 0,
        # M2-1：口径明确为「排班工钱」，与全口径（labor_m_full）分开说，差距就是老板看不见的钱
        "plain_text": "直接人工（排班工钱）占营业额 {value}",
    },
    {
        "code": "efficiency",
        "name": "人效",
        "layer": "derived",
        "unit": "元/小时",
        "formula": "revenue / hours",
        "depends_on": "revenue,hours",
        "inputs": "revenue",
        "aggregation": "ratio",
        "scope": "day",
        "benchmark_key": "efficiency",
        # M3-1（docs/03 §2）：快餐口径 120–180 元/工时。旧的 80–300 下限 80 是**正餐**标准，
        # 对快餐会**漏报**（本该报"人效偏低"的店被判成正常）。
        "warn_high": 180.0,
        "warn_low": 120.0,
        "higher_better": 1,
        "plain_text": "每投一小时换回 {value} 元",
    },
    {
        "code": "avg_ticket",
        "name": "客单价",
        "layer": "derived",
        "unit": "元",
        "formula": "revenue / orders",
        "depends_on": "revenue,orders",
        "inputs": "revenue,orders",
        "aggregation": "ratio",
        "scope": "day",
        "benchmark_key": "avg_ticket",
        "warn_high": 45.0,
        "warn_low": 15.0,
        "higher_better": 1,
        "plain_text": "平均每单 {value} 元",
    },
    {
        "code": "food_cost_rate",
        "name": "食材成本率",
        "layer": "derived",
        "unit": "ratio",
        "formula": "purchase / revenue",
        "depends_on": "purchase,revenue",
        "inputs": "purchase,revenue",
        "aggregation": "ratio",
        "scope": "day",
        "benchmark_key": "food_cost_rate",
        # M2-0（docs/02 §2）：区间 28%–38%，预警线相应改为 40%。旧值 0.22/0.18 会让所有快餐店报警。
        "warn_high": 0.40,
        "warn_low": 0.28,
        "higher_better": 0,
        "plain_text": "食材吃掉 {value}",
    },
    {
        "code": "breakeven_daily",
        "name": "保本营业额(日)",
        "layer": "derived",
        "unit": "元",
        # M2-1：人工从「排班工钱」换成「全口径月人工」，保本线才是真底线
        "formula": "(rent_m + utility_m + labor_m_full) / 30 / gross_margin",
        "depends_on": "labor_m_full,rent_m,utility_m,gross_margin",
        "inputs": "rent_m,utility_m,gross_margin",
        "aggregation": "ratio",
        "scope": "month",
        "benchmark_key": "breakeven_daily",
        "warn_high": None,
        "warn_low": None,
        "higher_better": None,
        "plain_text": "你每天至少要做 {value} 元才不亏",
    },
    # ---- M3-0：全口径人力成本率（第 6 个派生指标，docs/03 M3-0 授权）----
    #
    # ⚠️ 跨口径问题的解法（任务书 §2 M3-0 要求"自行解决并写清"）：
    #   方案 (a) `labor_m_full / (revenue_avg × 30)` 与
    #   方案 (b) `(labor_m_full / 30) / revenue_avg` 在数学上**完全等价**。
    #   我把 `revenue_avg × 30` 预计算成一个注入量 `revenue_m_avg`（区间日均×30），
    #   于是公式写成 `labor_m_full / revenue_m_avg` —— 与 (a)/(b) 同值。
    #
    #   为什么不直接把 `revenue_avg`（日均，元/天）当分母：那是个"每天多少元"的率，
    #   它的 aggregation 只能是 ratio，拿它做分母会被 M2-6 的守卫拦下。
    #   要绕过守卫就得把率标成 additive —— 那是为了让守卫闭嘴而撒谎。
    #   预乘 30 之后得到的是**金额**，标 additive 是真话。
    #
    #   口径实证：Y3 = 37,454.6 / (4,705.84 × 30) = 26.5%，对上 docs/01 §4 缺陷 2 的目标 26.5%。
    #   （若误用"锚点月营业额"当分母，Y3 会算成 28.9% —— 因为 9 月是淡季。）
    {
        "code": "labor_cost_rate_full",
        "name": "全口径人力成本率",
        "layer": "derived",
        "unit": "ratio",
        "formula": "labor_m_full / revenue_m_avg",
        "depends_on": None,
        "inputs": None,
        "aggregation": "ratio",
        "scope": "month",
        "benchmark_key": "labor_cost_rate",   # 复用同一区间（docs/03 M3-0）
        "warn_high": 0.22,
        "warn_low": 0.15,
        "higher_better": 0,
        "plain_text": "把社保、公积金、员工餐全算上，人工占营业额 {value}",
    },
    # ---- M3-3：账实差异率（第 7 个派生指标，docs/03 §3 M3-3 授权）----
    {
        "code": "stock_variance_rate",
        "name": "账实差异率",
        "layer": "derived",
        "unit": "ratio",
        "formula": "abs(stock_variance_amount) / stock_book_amount",
        "depends_on": None,
        "inputs": None,
        "aggregation": "ratio",
        # M4-2（docs/04 §1 裁定 6-6）：scope 从 day 改成 **period**。
        # 它的输入（盘面/实盘金额）是**跟查询区间走**的，标 day 语义不准。
        "scope": "period",
        "benchmark_key": None,
        "warn_high": None,
        "warn_low": None,
        "higher_better": 0,
        "plain_text": "账面和实盘差了 {value}",
    },
    # ---- M6（docs/06 §0 裁定 7）：可解释损耗率（第 8 个派生指标）----
    # 行业指标「可解释损耗率 = 已登记且通过审核的损耗金额 ÷ 总损耗金额」。
    # 这是 T01–T08 原因代码化 + M5-2 审批流**共同**换来的指标：
    #   * 只代码化不审批 → 自己填自己审，"已解释"是假的
    #   * 只审批不代码化 → 有签字但没法按环节聚合
    # 两者合起来，老板月底看到的是「损耗 8000，其中 6500 有原因、1500 说不清」，
    # 而不是干巴巴一个 8000 —— **能追责的才是能改进的**。
    # 分母为 0（这段没报损）时按引擎守卫返回 None，不返回 0（不给"100% 已解释"的假安心）。
    {
        "code": "explained_loss_rate",
        "name": "可解释损耗率",
        "layer": "derived",
        "unit": "ratio",
        "formula": "explained_loss_m / scrap_amount",
        "depends_on": None,
        "inputs": None,
        "aggregation": "ratio",
        # scope=period：报损是按日期区间统计的（与 scrap_amount 一致）
        "scope": "period",
        "benchmark_key": None,
        "warn_high": None,
        "warn_low": None,
        "higher_better": 1,
        "plain_text": "损耗里有 {value} 说得清是哪个环节",
    },
]

#: 已退役的指标 code（改名或废用的旧码）。
#:
#: 为什么需要这张表：播种是**按 code 幂等 upsert** 的，没有退役机制时，
#: `real_profit_gap` 改名成 `real_profit_m` 之后，旧行会留在库里继续被计算、
#: 继续出现在报告里（同一件事显示两遍）。所以每次播种显式把它们停用。
#:
#: **停用而不是删除** —— 与 docs/04 §2bis.4 第 3 条同一个道理：
#: 删了会断历史（`metric_value` 里按旧 code 存的快照会变成孤儿）。
RETIRED_METRIC_CODES: tuple[str, ...] = (
    # M4-1：改名 real_profit_gap → real_profit_m（旧名里的 gap 装的却是"实际利润"，易误读）
    "real_profit_gap",
)


#: 报告里核心指标的展示顺序。
#:
#: M1 施工单 §3.4 原表序是 5 个（labor_cost_rate / efficiency / avg_ticket /
#: food_cost_rate / breakeven_daily）。M3 授权新增 2 个，M6 授权新增 1 个，总数 5 → **8**：
#:   * `labor_cost_rate_full`   —— 依据 docs/03 §2 M3-0（补全口径，缺陷 2 的收尾）
#:   * `stock_variance_rate`    —— 依据 docs/03 §3 M3-3（账实差异，本产品护城河）
#:   * `explained_loss_rate`    —— 依据 docs/06 §0 裁定 7（可解释损耗率，M6）
CORE_METRIC_CODES: tuple[str, ...] = (
    "labor_cost_rate",
    "labor_cost_rate_full",
    "efficiency",
    "avg_ticket",
    "food_cost_rate",
    "breakeven_daily",
    "stock_variance_rate",
    "explained_loss_rate",
)

# 定义性列：这些变了才 bump version。enabled 是运行时开关，不算定义性变更。
_DEFINITION_COLUMNS = (
    "name",
    "layer",
    "unit",
    "formula",
    "depends_on",
    "benchmark_key",
    "warn_high",
    "warn_low",
    "higher_better",
    "plain_text",
    "aggregation",
    "scope",
    "inputs",
)
_FIELD_DEFINITION_COLUMNS = (
    "entity",
    "label",
    "data_type",
    "unit",
    "scope",
    "is_required",
    "default_value",
    "enum_values",
    "sort_order",
    "help_text",
)


def _fill_depends_on(rows: Sequence[dict[str, Any]]) -> None:
    """formula 改了却忘了改 depends_on 是典型的人祸，这里自动补齐并校正。

    depends_on 只放**指标**（不含 field_def 字段）—— 字段依赖由 inputs 表达，
    两条线分开才能让前端知道"新指标该摆在哪、要哪些输入框"。
    """
    metric_codes = {r["code"] for r in rows}
    for row in rows:
        formula = row.get("formula")
        if not formula:
            continue
        names = FormulaParser.names(FormulaParser.parse(formula))
        deps = sorted(n for n in names if n in metric_codes)
        row["depends_on"] = ",".join(deps) if deps else None
        inputs = sorted(n for n in names if n not in metric_codes)
        row["inputs"] = ",".join(inputs) if inputs else None


def seed_fields(session: Session) -> dict[str, str]:
    """返回 {code: 'inserted'|'updated'|'unchanged'}。"""
    result: dict[str, str] = {}
    for seed in FIELD_SEEDS:
        row = session.get(FieldDef, seed["code"])
        if row is None:
            session.add(FieldDef(**seed))
            result[seed["code"]] = "inserted"
            continue
        changed = False
        for col in _FIELD_DEFINITION_COLUMNS:
            if getattr(row, col) != seed.get(col):
                setattr(row, col, seed.get(col))
                changed = True
        result[seed["code"]] = "updated" if changed else "unchanged"
    session.flush()
    return result


def seed_metrics(session: Session) -> dict[str, str]:
    rows = [dict(r) for r in METRIC_SEEDS]
    _fill_depends_on(rows)

    # 先自检：种子里任何一条公式非法/引用不存在，就在这里炸，不要等到运行时
    FormulaParser.parse("1")  # 触发一次解析以确保解析器可用
    codes = {r["code"] for r in rows}
    for row in rows:
        formula = row.get("formula")
        if not formula:
            continue
        tree = FormulaParser.parse(formula)
        names = FormulaParser.names(tree)
        unknown = names - codes - {f["code"] for f in FIELD_SEEDS}
        if unknown:
            raise ValueError(f"种子指标 {row['code']} 引用了不存在的名字：{sorted(unknown)}")

    result: dict[str, str] = {}
    for seed in rows:
        row = session.get(MetricDef, seed["code"])
        if row is None:
            seed.setdefault("version", 1)
            session.add(MetricDef(**seed))
            result[seed["code"]] = "inserted"
            continue
        changed = False
        for col in _DEFINITION_COLUMNS:
            if getattr(row, col) != seed.get(col):
                setattr(row, col, seed.get(col))
                changed = True
        if changed:
            row.version = (row.version or 1) + 1
            result[seed["code"]] = "updated"
        else:
            result[seed["code"]] = "unchanged"

    # 退役旧 code：停用不删除（历史 metric_value 保留可查）
    for retired in RETIRED_METRIC_CODES:
        old = session.get(MetricDef, retired)
        if old is not None and old.enabled:
            old.enabled = 0
            result[retired] = "retired"

    session.flush()
    return result


_WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def seed_calendar(session: Session, start: date | None = None, end: date | None = None) -> int:
    """灌 dim_calendar。默认覆盖「去年 1 月 1 日 ~ 明年 12 月 31 日」。"""
    today = date.today()
    start = start or date(today.year - 1, 1, 1)
    end = end or date(today.year + 1, 12, 31)

    existing = {
        row for row in session.scalars(select(DimCalendar.date).where(DimCalendar.date >= start.isoformat()))
    }
    added = 0
    cursor = start
    while cursor <= end:
        key = cursor.isoformat()
        if key not in existing:
            iso_year, iso_week, _ = cursor.isocalendar()
            session.add(
                DimCalendar(
                    date=key,
                    year=cursor.year,
                    month=cursor.month,
                    day=cursor.day,
                    weekday=cursor.weekday(),
                    is_weekend=1 if cursor.weekday() >= 5 else 0,
                    iso_week=f"{iso_year}-W{iso_week:02d}",
                    label=f"{cursor.month}月{cursor.day}日 {_WEEKDAY_LABELS[cursor.weekday()]}",
                )
            )
            added += 1
        cursor += timedelta(days=1)
    session.flush()
    return added


def seed_all(session: Session, *, with_calendar: bool = True) -> dict[str, Any]:
    # 分类主数据是全局共享的（门店间共用一套），所以在这里播、不带 shop_id
    from .goods_master import seed_categories

    fields = seed_fields(session)
    metrics = seed_metrics(session)
    categories = seed_categories(session)
    calendar_added = seed_calendar(session) if with_calendar else 0
    session.commit()
    return {
        "fields": fields,
        "metrics": metrics,
        "categories": categories,
        "calendar_days_added": calendar_added,
    }
