"""Pydantic 出入参（施工单 3.2 / server/schemas.py）。

设计要点：**原子值一律走 `values: dict[code, value]`**，不写死 revenue / purchase / orders。
原因：施工单 3.3 的三张元数据表就是要把「有哪些字段」从代码里挪进表里，
如果这里写死字段名，插一行 field_def 就还得改 Pydantic + 前端 —— 架构就白做了。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
FIELD_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


def _check_time(v: str) -> str:
    if not isinstance(v, str) or not TIME_RE.match(v):
        raise ValueError("时间格式必须是 HH:MM（24 小时制），例如 18:00")
    return v


def _check_field_code(k: str) -> str:
    if not FIELD_CODE_RE.match(k):
        raise ValueError(f"字段 code 非法：{k}（只允许小写字母/数字/下划线）")
    return k


# --------------------------------------------------------------------------- #
# 门店
# --------------------------------------------------------------------------- #
class ShopCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64, description="店名，例：喜洋洋")
    category: str = Field(default="catering", max_length=32)
    default_hourly_rate: float = Field(default=25.0, ge=0, le=1000, description="默认时薪（元/小时）")
    note: str | None = None


class ShopUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    category: str | None = Field(default=None, max_length=32)
    #: M4-6 多店归属
    owner_key: str | None = Field(default=None, max_length=64)
    is_active: int | None = Field(default=None, ge=0, le=1)
    default_hourly_rate: float | None = Field(default=None, ge=0, le=1000)
    note: str | None = None


class ShopPatch(BaseModel):
    """多店场景下的轻量更新（与 ShopUpdate 同义，保留是为了不动 M1 的契约）。"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    owner_key: str | None = Field(default=None, max_length=64)
    is_active: int | None = Field(default=None, ge=0, le=1)
    note: str | None = None


class ShopOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    category: str
    #: M4-6：同一老板/法人的标识。为空 = 独立门店（不会被误并到别人店里）
    owner_key: str | None = None
    #: M4-6：关店的不进汇总，但历史保留可查
    is_active: int = 1
    default_hourly_rate: float
    note: str | None = None
    created_at: datetime


# --------------------------------------------------------------------------- #
# 工时
# --------------------------------------------------------------------------- #
class ShiftIn(BaseModel):
    worker_name: str = Field(min_length=1, max_length=32)
    start_time: str
    end_time: str
    hourly_rate: float | None = Field(default=None, ge=0, le=1000, description="不填则用门店默认时薪")

    @field_validator("start_time")
    @classmethod
    def _v_start(cls, v: str) -> str:
        return _check_time(v)

    @field_validator("end_time")
    @classmethod
    def _v_end(cls, v: str) -> str:
        return _check_time(v)


class ShiftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    worker_name: str
    start_time: str
    end_time: str
    hours: float
    hourly_rate: float | None = None


# --------------------------------------------------------------------------- #
# 录入
# --------------------------------------------------------------------------- #
class DailyUpsertIn(BaseModel):
    """一天一条，重复保存 = 覆盖（施工单 3.6）。"""

    shop_id: int
    stat_date: date
    values: dict[str, float | None] = Field(default_factory=dict, description="字段 code → 值（day 口径）")
    shifts: list[ShiftIn] = Field(default_factory=list)
    note: str | None = None

    @field_validator("values")
    @classmethod
    def _v_codes(cls, v: dict[str, float | None]) -> dict[str, float | None]:
        for k in v:
            _check_field_code(k)
        return v


class MonthlyUpsertIn(BaseModel):
    """月口径原子值（房租 / 水电 / 毛利率）。重复保存 = 覆盖。"""

    #: 路径已给出 shop_id 时可省略
    shop_id: int | None = None
    month: str = Field(description="YYYY-MM")
    values: dict[str, float | None] = Field(default_factory=dict)

    @field_validator("month")
    @classmethod
    def _v_month(cls, v: str) -> str:
        if not MONTH_RE.match(v):
            raise ValueError("month 必须是 YYYY-MM，例如 2026-10")
        return v


class FactOut(BaseModel):
    stat_date: str
    period_type: str
    values: dict[str, float | None] = Field(default_factory=dict)
    note: str | None = None


class DailyOut(FactOut):
    shifts: list[ShiftOut] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# M3-3 库存（goods / stocktakes / scraps）
# --------------------------------------------------------------------------- #
class StocktakeIn(BaseModel):
    shop_id: int
    goods_id: int
    take_date: date
    #: 账面数量：**可不填**（M4-4 起系统会自己推算）。
    #: 填了则作为"老板自报的账"保留，用来和系统推算值并列对照。
    book_qty: float | None = Field(default=None, ge=0)
    actual_qty: float = Field(ge=0)
    #: 不填则快照 goods.unit_price（主数据快照纪律：改商品单价不影响历史盘点）
    unit_price: float | None = Field(default=None, ge=0)
    #: M5-2 盘亏原因代码（T01–T08）。盘盈/盘亏超过阈值时**必填**
    reason_code: str | None = Field(default=None, max_length=8)
    #: M5-2 审批人。超阈值（±100 元或 ±5%）不填则拒绝提交
    approver: str | None = Field(default=None, max_length=32)
    note: str | None = None


class StocktakeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    goods_id: int
    take_date: date
    #: 老板自报的账面（可空）。M4-4 起系统自己推算，这一列退化为对照用
    book_qty: float | None = None
    #: 系统推算的账面快照
    computed_book_qty: float | None = None
    actual_qty: float
    unit_price: float
    reason_code: str | None = None
    reason_name: str | None = None
    approver: str | None = None
    note: str | None = None


class ScrapIn(BaseModel):
    shop_id: int
    goods_id: int
    scrap_date: date
    qty: float = Field(gt=0)
    units_lost: int = Field(default=0, ge=0, description="疑似丢单/少收的单数")
    unit_price: float | None = Field(default=None, ge=0)
    reason: str | None = Field(default=None, max_length=64)
    #: M5-2 损耗原因代码（T01–T08）。**必填** —— 自由文本不能聚合分析，等于白记
    reason_code: str | None = Field(default=None, max_length=8)
    #: M5-2 审批人。超阈值（±100 元或 ±5%）不填则拒绝提交
    approver: str | None = Field(default=None, max_length=32)
    photo_url: str | None = None
    note: str | None = None


class ScrapOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    goods_id: int
    scrap_date: date
    qty: float
    units_lost: int
    unit_price: float
    amount: float
    reason: str | None = None
    reason_code: str | None = None
    reason_name: str | None = None
    approver: str | None = None
    photo_url: str | None = None
    note: str | None = None


class LossReasonOut(BaseModel):
    """损耗原因代码字典（前端下拉直接用它渲染，加代码不用改前端）。"""

    code: str
    name: str


class LossReasonBreakdown(BaseModel):
    code: str
    name: str
    amount: float
    count: int
    share: float


class FieldOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    entity: str | None = None
    label: str | None = None
    data_type: str | None = None
    unit: str | None = None
    scope: str = "day"
    is_required: int = 0
    default_value: str | None = None
    enum_values: str | None = None
    sort_order: int = 0
    help_text: str | None = None


class MetricOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str | None = None
    layer: str | None = None
    unit: str | None = None
    formula: str | None = None
    depends_on: str | None = None
    aggregation: str = "ratio"
    scope: str = "day"
    benchmark_key: str | None = None
    warn_high: float | None = None
    warn_low: float | None = None
    higher_better: int | None = None
    plain_text: str | None = None
    version: int = 1


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
class BenchmarkVerdict(BaseModel):
    status: str                        # ok / high / low / none
    text: str
    range_display: str | None = None
    #: 区间来源（M2-0 要求：基准值必须写清从哪来，禁止凭感觉填）
    source: str | None = None
    # ---- M4-8：指标卡的区间条要画"你的位置"，所以必须把区间两端透出来 ----
    low: float | None = None
    high: float | None = None
    #: 你的位置在区间条上的相对坐标（0=区间左端，1=区间右端；越界会被截断但另给越界倍数）
    position: float | None = None


class MetricResult(BaseModel):
    code: str
    name: str | None = None
    value: float | None = None
    display: str = "--"
    unit: str | None = None
    layer: str | None = None
    sentence: str | None = None        # plain_text 渲染后的大白话
    benchmark: BenchmarkVerdict | None = None


class ConclusionOut(BaseModel):
    lines: list[str] = Field(default_factory=list)      # 严格三行：数字 / 判断 / 动作
    warnings: list[str] = Field(default_factory=list)


class PeriodOut(BaseModel):
    start: str
    end: str
    days_with_data: int
    days_span: int
    month: str
    #: 话术档位（M2-2）：day = 日/周，month = 月/季，year = 年度
    tier: str = "day"


class TrendItem(BaseModel):
    """环比一项（M2-3）：与**上一等长区间**对比。

    硬纪律：环比必须先 Σ 原子再算比率，不得对日率取平均 ——
    上一期与本期都是各自独立跑一遍引擎得到的，天然满足。
    """

    code: str
    name: str
    current: float | None = None
    previous: float | None = None
    delta: float | None = None          # 当前 - 上一期
    direction: str = "平"                # 升 / 平 / 降
    sentence: str | None = None          # 大白话，如「食材占比这一年在涨（30% → 41%）」


class ReportOut(BaseModel):
    shop: ShopOut
    period: PeriodOut
    metrics: list[MetricResult]
    conclusion: ConclusionOut
    observations: dict[str, Any] = Field(default_factory=dict)
    # 被引擎判为「算不出」的指标（分母缺失/为 0），用于前端提示填什么
    blocked: list[dict[str, str]] = Field(default_factory=list)
    #: 环比段（M2-3）。首期（没有上一期）时为空列表，不报错。
    trend: list[TrendItem] = Field(default_factory=list)
    #: M4-9：异常清单，**按金额降序**（老板先看钱最多的那个）
    anomalies: list["AnomalyItem"] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# M4-3 存货分类树
# --------------------------------------------------------------------------- #
class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    parent_code: str | None = None
    name: str
    level: int
    sort_order: int = 0
    is_custom: int = 0
    enabled: int = 1


class CategoryCreate(BaseModel):
    parent_code: str = Field(description="挂到哪个一级分类下，如 '02'")
    name: str = Field(min_length=1, max_length=32)
    sort_order: int = 100


# --------------------------------------------------------------------------- #
# M4-10 / M4-11 / M4-12 商品主数据
# --------------------------------------------------------------------------- #
class GoodsCreate(BaseModel):
    shop_id: int
    code: str | None = Field(default=None, max_length=32, description="留空则由 分类+序号+规格 自动生成")
    name: str = Field(min_length=1, max_length=64, description="物料名，不含规格")
    spec: str | None = Field(default=None, max_length=32, description="规格型号，如 '5L/桶'")
    spec_code: str | None = Field(default=None, max_length=4, description="规格码两位，如 '01'")
    barcode: str | None = Field(default=None, max_length=32)
    category_code: str = Field(description="挂**二级**分类，如 '02-01'")
    unit: str = Field(default="斤", max_length=16, description="库存单位/基准单位")
    purchase_unit: str | None = Field(default=None, max_length=16, description="进货单位，如 '箱'")
    usage_unit: str | None = Field(default=None, max_length=16, description="领用单位，如 '勺'")
    unit_conversion: float = Field(default=1.0, gt=0, le=100000,
                                   description="1 进货单位 = N 库存单位。**必填**")
    stock_to_usage: float = Field(default=1.0, gt=0, le=100000, description="1 库存单位 = N 领用单位")
    is_weighed: int = Field(default=0, ge=0, le=1, description="称重品：验收必须录实际净重")
    unit_price: float = Field(default=0.0, ge=0, description="元/库存单位")
    abc_class: str = Field(default="C", pattern="^[ABC]$")
    note: str | None = None
    #: M4-12：查重弹窗里，创建人点过"确认不是重复"之后传 true 才能落库
    confirmed_not_duplicate: bool = Field(default=False)


class GoodsUpdate(BaseModel):
    #: ⚠ 编码理论上可以改，但**被单据引用过就永远拒绝**（红线，见 goods_master.block_code_change）。
    #: 放在这里而不是静默忽略，是为了让"改编码"这个动作得到明确的 409 而不是变成无声的空操作。
    code: str | None = Field(default=None, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=64)
    spec: str | None = Field(default=None, max_length=32)
    barcode: str | None = Field(default=None, max_length=32)
    category_code: str | None = None
    unit: str | None = Field(default=None, max_length=16)
    purchase_unit: str | None = Field(default=None, max_length=16)
    usage_unit: str | None = Field(default=None, max_length=16)
    unit_conversion: float | None = Field(default=None, gt=0, le=100000)
    stock_to_usage: float | None = Field(default=None, gt=0, le=100000)
    is_weighed: int | None = Field(default=None, ge=0, le=1)
    unit_conversion_change_note: str | None = None
    unit_price: float | None = Field(default=None, ge=0)
    abc_class: str | None = Field(default=None, pattern="^[ABC]$")
    enabled: int | None = Field(default=None, ge=0, le=1)
    note: str | None = None


class GoodsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    shop_id: int
    code: str
    name: str
    spec: str | None = None
    spec_code: str | None = None
    barcode: str | None = None
    category_code: str | None = None
    category_path: str | None = None
    unit: str
    purchase_unit: str | None = None
    usage_unit: str | None = None
    unit_conversion: float = 1.0
    stock_to_usage: float = 1.0
    is_weighed: int = 0
    unit_conversion_changed_at: datetime | None = None
    unit_conversion_change_note: str | None = None
    unit_price: float = 0.0
    abc_class: str = "C"
    merged_into_id: int | None = None
    is_referenced: int = 0
    enabled: int = 1
    note: str | None = None

    #: 三单位制里的"库存单位"，就是 M3 的 unit —— 对外给别名，不重复存列
    @computed_field  # type: ignore[prop-decorator]
    @property
    def stock_unit(self) -> str:
        return self.unit

    #: 三单位制里的"1 进货单位 = N 库存单位"，就是 unit_conversion —— 同上
    @computed_field  # type: ignore[prop-decorator]
    @property
    def purchase_to_stock(self) -> float:
        return self.unit_conversion

    #: 命名规范：名称 + 规格 + 单位（docs/04 §2bis.1）
    @computed_field  # type: ignore[prop-decorator]
    @property
    def display_name(self) -> str:
        parts = [self.name]
        if self.spec:
            parts.append(self.spec)
        return " ".join(parts)


class DuplicateCandidate(BaseModel):
    goods_id: int
    code: str
    name: str
    spec: str | None = None
    unit: str | None = None
    category_path: str | None = None
    score: float = Field(description="相似度 0–1")
    reason: str = Field(description="为什么会提示：同名不同规格 / 名称高度相似 / 命中别名")


class DuplicateCheckOut(BaseModel):
    candidates: list[DuplicateCandidate] = Field(default_factory=list)
    threshold: float = 0.75
    #: 存在"高度相似（同名、规格也相同）"时，必须人工确认才能建
    must_confirm: bool = False


class GoodsMergeIn(BaseModel):
    source_ids: list[int] = Field(min_length=1, description="要合并掉的副码")
    target_id: int = Field(description="保留的主码")
    note: str | None = None


class GoodsMergeOut(BaseModel):
    target_id: int
    target_code: str
    merged: list[str] = Field(default_factory=list, description="被停用并指向主码的副码")
    moved_records: int = Field(default=0, description="归集到主码的历史单据数")


class AliasIn(BaseModel):
    alias: str = Field(min_length=1, max_length=64)
    note: str | None = None


class AliasOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    goods_id: int
    alias: str
    note: str | None = None


# --------------------------------------------------------------------------- #
# M4-4 采购 / 入库
# --------------------------------------------------------------------------- #
class PurchaseIn(BaseModel):
    shop_id: int
    goods_id: int
    purchase_date: date
    qty: float = Field(gt=0, description="按**进货单位**计的数量")
    unit: str | None = Field(default=None, max_length=16, description="留空取商品的进货单位")
    unit_price: float | None = Field(default=None, ge=0, description="元/进货单位；留空取商品单价×换算率")
    actual_net_qty: float | None = Field(
        default=None, ge=0,
        description="称重品的**实际净重**（库存单位）。称重品必填，不接受默认换算",
    )
    #: M5-1 欠哪家供应商（留空 = 现结）。填了会按账期自动生成应付
    supplier_id: int | None = None
    #: M5-2 折价：货不对板时供应商让掉的数量与金额（超阈值需审批）
    discount_qty: float = Field(default=0.0, ge=0)
    discount_amount: float = Field(default=0.0, ge=0)
    approver: str | None = Field(default=None, max_length=32)
    note: str | None = None


class PurchaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    goods_id: int
    purchase_date: date
    qty: float
    unit: str
    unit_conversion: float
    unit_price: float
    qty_in_stock_unit: float
    amount: float
    category_code: str | None = None
    supplier_id: int | None = None
    supplier_name: str | None = None
    discount_qty: float = 0.0
    discount_amount: float = 0.0
    approver: str | None = None
    note: str | None = None


class BulkResult(BaseModel):
    inserted: int = 0
    updated: int = 0
    errors: list[str] = Field(default_factory=list)


class ReceiptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    goods_id: int
    receipt_date: date
    qty: float
    unit: str
    unit_conversion: float
    unit_price: float
    qty_in_stock_unit: float
    amount: float
    #: 称重品的实际净重（库存单位）。非称重品为 None
    actual_net_qty: float | None = None
    accepted: int = 1
    note: str | None = None


# --------------------------------------------------------------------------- #
# M4-3 分类成本结构
# --------------------------------------------------------------------------- #
class CategoryCostItem(BaseModel):
    code: str
    name: str
    level: int
    amount: float
    share: float
    previous_amount: float | None = None
    previous_share: float | None = None
    share_delta: float | None = None
    direction: str = "平"
    is_main_driver: bool = False


class CategoryCostOut(BaseModel):
    total: float
    coverage: float = Field(description="已归类金额 / 总采购金额，用来说明数据完整度")
    period: dict[str, str]
    previous_period: dict[str, str] | None = None
    items: list[CategoryCostItem] = Field(default_factory=list)
    conclusion: str | None = None


# --------------------------------------------------------------------------- #
# M4-6 / M4-7 多店
# --------------------------------------------------------------------------- #


class ShopCompareItem(BaseModel):
    shop_id: int
    shop_name: str
    revenue: float | None = None
    labor_cost_rate_full: float | None = None
    food_cost_rate: float | None = None
    efficiency: float | None = None
    breakeven_daily: float | None = None
    real_profit_m: float | None = None
    daily_avg_revenue: float | None = None
    rank: dict[str, int] = Field(default_factory=dict)


class PortfolioOut(BaseModel):
    owner_key: str | None = None
    period: PeriodOut
    shop_count: int = 0
    #: 汇总指标：**一律重算**（Σ原子再算比率），不是对各店比率取平均
    metrics: dict[str, float | None] = Field(default_factory=dict)
    total_revenue: float | None = None
    total_labor_cost: float | None = None
    total_real_profit: float | None = None
    shops: list[ShopCompareItem] = Field(default_factory=list)
    best_shop: dict[str, str] | None = None
    worst_shop: dict[str, str] | None = None
    anomalies: list[dict[str, Any]] = Field(default_factory=list)
    note: str | None = None


# --------------------------------------------------------------------------- #
# M4-8 / M4-9 视觉所需
# --------------------------------------------------------------------------- #
class AnomalyItem(BaseModel):
    """异常清单的一条。**按金额降序**——老板先看钱最多的那个。"""

    code: str
    name: str
    status: str                      # high / low / warn
    display: str
    amount: float = 0.0              # 折算成钱的严重度，用于排序
    sentence: str | None = None


class MonthPoint(BaseModel):
    month: str                       # 'YYYY-MM'
    revenue: float | None = None
    breakeven_daily: float | None = None
    daily_avg_revenue: float | None = None


class MonthlySeriesOut(BaseModel):
    points: list[MonthPoint] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 元数据
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# M5-0 · BOM / 成本卡（docs/05 §3）
# --------------------------------------------------------------------------- #
class BomIn(BaseModel):
    shop_id: int
    dish_name: str = Field(max_length=64)
    goods_id: int
    #: 标准**净**用量（BOM 上写的量）
    std_qty_net: float = Field(gt=0)
    #: 出成率（0,1]。留空按 1.0（不损耗）—— 但路由会提示"标准成本可能算低"
    yield_rate: float | None = Field(default=None, gt=0, le=1.0)
    unit: str | None = Field(default=None, max_length=16, description="必须等于商品的库存单位（不一致会被拒）")
    std_price: float | None = Field(
        default=None, ge=0,
        description="标准价，**按毛料价填**（你从供应商那买的价）。留空取商品单价",
    )
    note: str | None = None


class BomOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    dish_name: str
    goods_id: int
    goods_name: str | None = None
    std_qty_net: float
    yield_rate: float
    #: 标准毛用量 = 净用量 ÷ 出成率（后端算，不接受传入）
    std_qty_gross: float
    unit: str
    std_price: float
    #: 单份标准成本 = 标准毛料用量 × 标准价（毛料价）。
    #: 给老板一个能直接核对的数："这道菜一份按配方应该花多少"。
    #: 行业口径是 净料单价 = 毛料单价 ÷ 净料率 —— 我们存的是毛料价，
    #: 配毛料用量，两者相乘才是自洽的标准成本（M5 自查审查项 A3）。
    std_cost: float = 0.0
    abc_class: str = "C"
    version: int = 1
    effective_from: date | None = None
    enabled: int = 1
    note: str | None = None
    #: 出成率没填时的告警（填了出成率才没有）
    warning: str | None = None


class BomVarianceIn(BaseModel):
    shop_id: int
    biz_date: date
    dish_name: str = Field(max_length=64)
    goods_id: int
    #: 卖出份数
    sold_qty: int = Field(gt=0)
    #: 实际领用/耗用数量（库存单位）。**留空则由系统从盘点反推**
    #: （期初实盘 + 入库 − 期末实盘 − 报损），老板不必多录一个数
    actual_usage: float | None = Field(default=None, ge=0)
    #: 实际价（元/库存单位）；留空按标准价 → 价差为 0
    actual_price: float | None = Field(default=None, ge=0)
    note: str | None = None


class BomVarianceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    biz_date: date
    dish_name: str
    goods_id: int
    goods_name: str | None = None
    sold_qty: int
    std_usage: float
    actual_usage: float
    std_price: float
    actual_price: float
    qty_variance: float
    qty_variance_amount: float
    price_variance_amount: float
    abc_class: str = "C"
    note: str | None = None


class BomVarianceSummaryOut(BaseModel):
    """量差 / 价差汇总。两者分开给 —— 量差找厨房、价差找采购。"""

    has_data: bool = False
    qty_variance_amount: float = 0.0
    price_variance_amount: float = 0.0
    total_variance_amount: float = 0.0
    dishes: list[dict[str, Any]] = Field(default_factory=list)
    period: dict[str, str] | None = None
    conclusion: str | None = None


class CountingDueItem(BaseModel):
    goods_id: int
    code: str | None = None
    name: str
    spec: str | None = None
    abc_class: str = "C"
    cycle_days: int
    last_take_date: str | None = None
    days_since_last: int | None = None
    due: bool = False


class CountingDueOut(BaseModel):
    """ABC 抽盘：本期该盘哪些货。"""

    as_of: str
    due_count: int = 0
    items: list[CountingDueItem] = Field(default_factory=list)
    note: str | None = None


# --------------------------------------------------------------------------- #
# M5-1 · 供应商与应付账款（docs/05 §4）
# --------------------------------------------------------------------------- #
class SupplierIn(BaseModel):
    shop_id: int
    name: str = Field(max_length=64)
    main_contact: str | None = Field(default=None, max_length=32)
    main_phone: str | None = Field(default=None, max_length=32)
    backup_contact: str | None = Field(default=None, max_length=32)
    backup_phone: str | None = Field(default=None, max_length=32)
    #: 账期（天）：货到后多少天内付款。0 = 现结
    credit_days: int = Field(default=0, ge=0)
    credit_limit: float = Field(default=0.0, ge=0)
    photo_url: str | None = None
    on_time_deliveries: int = Field(default=0, ge=0)
    total_deliveries: int = Field(default=0, ge=0)
    qualified_batches: int = Field(default=0, ge=0)
    total_batches: int = Field(default=0, ge=0)
    delivery_lead_days: float = Field(default=0.0, ge=0)
    note: str | None = None


class SupplierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    main_contact: str | None = None
    main_phone: str | None = None
    backup_contact: str | None = None
    backup_phone: str | None = None
    credit_days: int = 0
    credit_limit: float = 0.0
    on_time_deliveries: int = 0
    total_deliveries: int = 0
    qualified_batches: int = 0
    total_batches: int = 0
    delivery_lead_days: float = 0.0
    enabled: int = 1
    note: str | None = None


class SupplierRatingOut(BaseModel):
    """评级 = 准时 30% + 质量 30% + 价格稳定 20% + 损耗 20%（v0.2 §3.1.1 照抄）。"""

    supplier_id: int
    name: str
    buy_amount: float = 0.0
    purchase_count: int = 0
    score: float
    grade: str
    breakdown: dict[str, float] = Field(default_factory=dict)
    loss_amount: float = 0.0
    credit_days: int = 0
    credit_limit: float = 0.0
    period: dict[str, str] | None = None


class PayableIn(BaseModel):
    shop_id: int
    supplier_id: int
    purchase_id: int | None = None
    biz_date: date
    #: 到期日。留空按供应商账期自动算（= biz_date + credit_days）
    due_date: date | None = None
    amount: float = Field(gt=0)
    discount_qty: float = Field(default=0.0, ge=0)
    discount_amount: float = Field(default=0.0, ge=0)
    paid_amount: float = Field(default=0.0, ge=0)
    note: str | None = None


class PayableOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    supplier_id: int
    supplier_name: str | None = None
    purchase_id: int | None = None
    biz_date: date
    due_date: date
    amount: float
    discount_qty: float = 0.0
    discount_amount: float = 0.0
    paid_amount: float = 0.0
    payable_amount: float = 0.0
    outstanding: float = 0.0
    overdue_days: int | None = None
    note: str | None = None


class AgingBucketOut(BaseModel):
    bucket: str
    amount: float
    count: int


class PayablesAgingOut(BaseModel):
    """应付账龄 + 逾期清单 —— 任务书 §4 验收要的那句「欠 3 家共 X 元…」。"""

    as_of: str
    supplier_count: int = 0
    outstanding_total: float = 0.0
    overdue_total: float = 0.0
    overdue_count: int = 0
    aging: list[AgingBucketOut] = Field(default_factory=list)
    overdue_items: list[dict[str, Any]] = Field(default_factory=list)
    upcoming_items: list[dict[str, Any]] = Field(default_factory=list)
    sentence: str | None = None


class ConcentrationOut(BaseModel):
    """供应商集中度：TOP1 占比。太集中断供风险大，太散议价弱。"""

    total: float = 0.0
    top1_share: float | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    period: dict[str, str] | None = None
    note: str | None = None
