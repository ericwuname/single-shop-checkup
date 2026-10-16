"""ORM 定义（施工单 3.2 / server/models.py）。

表集合严格等于施工单列举的 7 张表，**M1 期间不新增业务表**（风险清单 #3）：

    shops / fact_daily / shifts              ← 事实层
    field_def / metric_def / metric_value    ← 元数据层（扩展能力的地基）
    dim_calendar                             ← 日历维

两处工程决定（如实标注，供蓝图窗口审查）
----------------------------------------
A. ``fact_daily`` 采用「类型化列 + JSON 溢出」双通道：
   6 个内建原子字段有独立列（SQL 里肉眼可读、可核对）；其余 field_def 定义的字段
   落在 ``extra_json``。新增**字段**不需要改 schema，只需插 field_def 一行。
   取值优先级：类型化列 > extra_json，写入路径保证不双写。
B. ``field_def`` / ``metric_def`` 在施工单列出的列之外**追加了扩展列**
   （field_def.scope；metric_def.aggregation / scope / inputs）。
   原因见 §3.3 与 docs 报告：施工单给定的 ``derived_metric_as_divisor`` 守卫
   若按字面实现，会把它自己列出的 efficiency（revenue / hours）也判死，
   因此必须让引擎知道「这个派生指标可不可加」。施工单原列一个未删。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    # M1 单机单用户，直接用本地时间，不引入时区库
    return datetime.now()


# --------------------------------------------------------------------------- #
# 事实层
# --------------------------------------------------------------------------- #
class Shop(Base):
    """门店。M1 只有单店，但主键从第一天就是 shop_id，避免 M2 多店时动地基。

    M4-6 加两列（**不加表**）：`owner_key` 把同一个老板的几家店归到一起，
    `is_active` 让关掉的店不进汇总。
    """

    __tablename__ = "shops"
    __table_args__ = (Index("ix_shops_owner", "owner_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="catering")
    #: 同一老板 / 同一法人的标识。为空时按"独立门店"处理（不会误并到别人店里）
    owner_key: Mapped[str | None] = mapped_column(String(64))
    #: 关店的不进汇总，但历史数据保留可查（与"停用 ≠ 删除"同一个道理）
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 老板没给时薪时的兜底值（元/小时）
    default_hourly_rate: Mapped[float] = mapped_column(Float, nullable=False, default=25.0)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class FactDaily(Base):
    """原子事实行。

    period_type='day'   → stat_date 就是那一天，值按天填
    period_type='month' → stat_date 固定为该月 1 号，装月口径原子值（房租/水电/毛利率）

    之所以不新开 fact_monthly 表：施工单风险清单 #3 明确「M1 期间禁止加业务表」。
    """

    __tablename__ = "fact_daily"
    __table_args__ = (
        UniqueConstraint("shop_id", "stat_date", "period_type", name="uq_fact_daily_key"),
        Index("ix_fact_daily_shop_date", "shop_id", "stat_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    stat_date: Mapped[date] = mapped_column(Date, nullable=False)
    period_type: Mapped[str] = mapped_column(String(8), nullable=False, default="day")

    # ---- 6 个内建原子字段（类型化列） ----
    revenue: Mapped[float | None] = mapped_column(Float)          # 营业额（日）
    purchase: Mapped[float | None] = mapped_column(Float)         # 买菜/进货（日）
    orders: Mapped[float | None] = mapped_column(Float)           # 订单数（日）
    rent_m: Mapped[float | None] = mapped_column(Float)           # 月房租
    utility_m: Mapped[float | None] = mapped_column(Float)        # 月水电
    gross_margin: Mapped[float | None] = mapped_column(Float)     # 毛利率（比例，输入项）

    # ---- 溢出通道：field_def 里新增的字段落这里，无需改 schema ----
    extra_json: Mapped[str | None] = mapped_column(Text)

    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, onupdate=_now)


class Shift(Base):
    """排班/工时行。hours 由 start/end 算出并落库，便于 SQL 核对。

    工时为空 → 不写入（施工单 3.6）。跨夜 22:00→06:00 记 8 小时。
    """

    __tablename__ = "shifts"
    __table_args__ = (Index("ix_shifts_shop_date", "shop_id", "work_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    worker_name: Mapped[str] = mapped_column(String(32), nullable=False)
    start_time: Mapped[str] = mapped_column(String(5), nullable=False)   # 'HH:MM'
    end_time: Mapped[str] = mapped_column(String(5), nullable=False)     # 'HH:MM'
    hours: Mapped[float] = mapped_column(Float, nullable=False)
    hourly_rate: Mapped[float | None] = mapped_column(Float)             # 空 = 用门店默认值
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


# --------------------------------------------------------------------------- #
# 元数据层（施工单 3.3，列名与顺序按施工单，扩展列附在末尾）
# --------------------------------------------------------------------------- #
class FieldDef(Base):
    """原子字段注册表。新增字段 = 插一行，不改 Python / 不改前端。"""

    __tablename__ = "field_def"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    entity: Mapped[str | None] = mapped_column(Text)             # fact_daily / shift / shop
    label: Mapped[str | None] = mapped_column(Text)
    data_type: Mapped[str | None] = mapped_column(Text)          # number / integer / ratio / text
    unit: Mapped[str | None] = mapped_column(Text)               # 元 / 单 / 比例
    is_required: Mapped[int] = mapped_column(Integer, default=0)
    default_value: Mapped[str | None] = mapped_column(Text)
    enum_values: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[int] = mapped_column(Integer, default=1)

    # --- 扩展列（施工单未列，新增，不改动原列语义）---
    scope: Mapped[str] = mapped_column(Text, default="day")      # day / month
    help_text: Mapped[str | None] = mapped_column(Text)


class MetricDef(Base):
    """指标注册表。新增指标 = 插一行（验收硬门禁 #3）。"""

    __tablename__ = "metric_def"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    layer: Mapped[str | None] = mapped_column(Text)              # atomic / derived
    unit: Mapped[str | None] = mapped_column(Text)               # ratio / 元 / 小时 / 单
    formula: Mapped[str | None] = mapped_column(Text)
    depends_on: Mapped[str | None] = mapped_column(Text)         # 逗号分隔的 **指标** code
    benchmark_key: Mapped[str | None] = mapped_column(Text)
    warn_high: Mapped[float | None] = mapped_column(Float)
    warn_low: Mapped[float | None] = mapped_column(Float)
    higher_better: Mapped[int | None] = mapped_column(Integer)
    plain_text: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    version: Mapped[int] = mapped_column(Integer, default=1)

    # --- 扩展列（施工单未列，新增）---
    # additive = 可跨期相加（工时、人力成本）；ratio = 比率，跨期只能"先加原子再算"
    aggregation: Mapped[str] = mapped_column(Text, default="ratio")
    scope: Mapped[str] = mapped_column(Text, default="day")      # day / month
    inputs: Mapped[str | None] = mapped_column(Text)             # 逗号分隔的 **字段** code


class MetricValue(Base):
    """指标值缓存表（施工单 3.3，主键与列名完全一致）。"""

    __tablename__ = "metric_value"

    shop_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_start: Mapped[str] = mapped_column(Text, primary_key=True)
    period_end: Mapped[str] = mapped_column(Text, primary_key=True)
    metric_code: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[float | None] = mapped_column(Float)
    computed_at: Mapped[str | None] = mapped_column(Text)


class DimCalendar(Base):
    """日历维。用途：周汇总的第几周、周末/工作日拆分、后续节假日标记。"""

    __tablename__ = "dim_calendar"

    date: Mapped[str] = mapped_column(Text, primary_key=True)     # ISO 'YYYY-MM-DD'
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    day: Mapped[int] = mapped_column(Integer, nullable=False)
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)  # 0=周一 … 6=周日
    is_weekend: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    iso_week: Mapped[str] = mapped_column(Text, nullable=False)     # 'YYYY-Www'
    label: Mapped[str] = mapped_column(Text, nullable=False)        # '10月16日 周五'


# --------------------------------------------------------------------------- #
# M3 首次解锁的三张表
#
# 授权依据：`docs/03-M3长程施工任务书.md` §3 M3-3
#   goods / stocktakes / scraps  → 解锁（M3 可建）
#   suppliers / purchases / receipts / bom → 仍锁定（M3b / M4）
#
# 范围守卫（tests/test_metrics_engine.py）由「恰好 7 张」改为「恰好 10 张」，
# 锁定表名仍然禁止出现 —— 摩擦保留。
# --------------------------------------------------------------------------- #
class Goods(Base):
    """商品主数据（M3 建表；M4-3 / M4-10 / M4-11 / M4-12 扩充）。

    ## 命名决策（避免同一件事两个列，也避免两个概念一个名）

    * M3 的 `conversion_rate` **改名为 `unit_conversion`** —— 它就是 M4-3 要的换算率、
      也是 M4-11 三单位制里的 `purchase_to_stock`。同一个量只留一列，不给"漂移"留机会。
      （改名迁移见 `server/db.py::migrate_renames`）
    * M3 的 `category`（A/B/C **ABC 分类**）**改名为 `abc_class`** ——
      否则会和 M4-3 新增的 `category_code`（**存货分类树**，形如 `02-01-001`）撞概念。
    * `unit`（M3）= 三单位制里的**库存单位（基准单位）**，对外别名 `stock_unit`；
      真正新增的是进货单位、领用单位与两个换算系数。
    """

    __tablename__ = "goods"
    __table_args__ = (
        UniqueConstraint("shop_id", "code", name="uq_goods_shop_code"),
        Index("ix_goods_shop", "shop_id"),
        Index("ix_goods_category", "shop_id", "category_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)      # 店内自编号，如 'YOU'
    name: Mapped[str] = mapped_column(String(64), nullable=False)      # '色拉油'

    # ---- M4-3：存货分类（挂**二级**分类，形如 '02-01'）----
    category_code: Mapped[str | None] = mapped_column(String(16))
    # 分类路径缓存（'肉禽蛋类 / 猪肉'），列表与报表直接用，省一次 join
    category_path: Mapped[str | None] = mapped_column(String(64))

    # ---- M4-10：规格型号（SKU 层）。同名不同规格必须拆成两个编码 ----
    spec: Mapped[str | None] = mapped_column(String(32))        # '5L/桶'
    spec_code: Mapped[str | None] = mapped_column(String(4))    # '01' 规格两位
    barcode: Mapped[str | None] = mapped_column(String(32))     # 不同规格可有不同条码

    # ---- M4-11：三单位制 ----
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="斤")   # = 库存单位（基准）
    purchase_unit: Mapped[str | None] = mapped_column(String(16))                 # 箱
    usage_unit: Mapped[str | None] = mapped_column(String(16))                    # 勺 / 份
    #: 1 进货单位 = N 库存单位。**必填**（docs/04 §8：留空则数量对比全是假的）
    unit_conversion: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    #: 1 库存单位 = N 领用单位
    stock_to_usage: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    #: 称重品：验收端**必须录实际净重**，不接受默认换算（把包装当实重是高频错因）
    is_weighed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 换算系数变更留痕（供应商改包装是高频事件，改了不记就对不上账）
    unit_conversion_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    unit_conversion_change_note: Mapped[str | None] = mapped_column(Text)

    unit_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # 元/库存单位
    #: M5-2 定价审批：改价要留痕并留审批人（改价是采购里最容易出问题的动作）。
    #: ⚠️ 这里只留**最后一次**改价；完整改价历史需要一张审批流水表，本轮未授权建表（见交付报告）。
    unit_price_prev: Mapped[float | None] = mapped_column(Float)
    unit_price_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    unit_price_change_reason: Mapped[str | None] = mapped_column(Text)
    unit_price_approver: Mapped[str | None] = mapped_column(String(32))
    #: ABC 分类（蓝图 v1.0 §3：A 每周盘、B 每月、C 每季 —— 只盘 A 类能抓 80% 差异）
    abc_class: Mapped[str] = mapped_column(String(1), nullable=False, default="C")

    # ---- M4-12：一物多码治理 ----
    #: 被合并到哪个主编码（非空 = 本码是副码，已停用，历史数据自动归集到主码）
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("goods.id", ondelete="SET NULL"))
    #: 是否被业务数据引用过（采购/入库/盘点/报废）。引用过则**永不改编码**，只能停用后新建
    is_referenced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class GoodsCategory(Base):
    """存货分类树（M4-3，docs/04 §2）。

    **只拆两级**：一级 ≈9 个（蔬菜/肉禽蛋/水产/粮油/调料/酒水饮料/包材/清洁/营销物料），
    二级 ≈44 个。三级分类对夫妻店是灾难 —— 店员零意识，选不动。

    编码：`01` ~ `09`；中类 `01-01` ~ `09-05`。编码里**不写供应商、不写门店**。
    """

    __tablename__ = "goods_categories"
    __table_args__ = (Index("ix_goods_categories_level", "level", "sort_order"),)

    code: Mapped[str] = mapped_column(String(8), primary_key=True)      # '01' 或 '02-01'
    parent_code: Mapped[str | None] = mapped_column(String(8))          # 一级为 None
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)          # 1 / 2
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 老板自己加的分类（预置覆盖 80%，剩下让他加）
    is_custom: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class GoodsAlias(Base):
    """别名表（M4-12）：异名同物。

    「番茄 = 西红柿」这类靠名称相似度**查不出来**，必须显式维护。
    查询与报表按别名归集到主商品，避免同一件东西被算成两个码。
    """

    __tablename__ = "goods_aliases"
    __table_args__ = (
        UniqueConstraint("shop_id", "alias", name="uq_goods_alias_shop_name"),
        Index("ix_goods_aliases_goods", "goods_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    goods_id: Mapped[int] = mapped_column(ForeignKey("goods.id", ondelete="CASCADE"), nullable=False)
    alias: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class Purchase(Base):
    """采购单（M4-4 解锁）。

    与 `receipts` 的分工：采购单是"我订了什么"，入库验收单是"实际到了多少、多少斤"。
    小店的现实往往是两单合一，但**分开建才能容纳"订 10 箱到 9 箱"**这种最常见的差异。
    """

    __tablename__ = "purchases"
    __table_args__ = (Index("ix_purchases_shop_date", "shop_id", "purchase_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    goods_id: Mapped[int] = mapped_column(ForeignKey("goods.id", ondelete="CASCADE"), nullable=False)
    purchase_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: 按**进货单位**记数量（人只接触自己环节的单位，机器负责折算）
    qty: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="斤")
    #: 主数据快照：录单当时的换算率与单价，之后改商品不影响历史
    unit_conversion: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)   # 元/进货单位
    #: 折算成库存单位的数量（= qty × unit_conversion），入库/库存推算直接用这个
    qty_in_stock_unit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: 分摊到的分类（二级），用于成本结构表；等于商品当时的分类快照
    category_code: Mapped[str | None] = mapped_column(String(16))
    #: M5-1：这笔采购欠哪家供应商（空 = 现结/未登记供应商）
    supplier_id: Mapped[int | None] = mapped_column(
        ForeignKey("suppliers.id", ondelete="SET NULL")
    )
    #: M5-2 折价审批：货不对板时供应商让掉的数量与金额
    discount_qty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    discount_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: M5-2 超阈值（±100 元或 ±5%）时必须有审批人
    approver: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class Receipt(Base):
    """入库验收单（M4-4 解锁）。账面库存要靠它才能自动推算。"""

    __tablename__ = "receipts"
    __table_args__ = (Index("ix_receipts_shop_date", "shop_id", "receipt_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    goods_id: Mapped[int] = mapped_column(ForeignKey("goods.id", ondelete="CASCADE"), nullable=False)
    receipt_date: Mapped[date] = mapped_column(Date, nullable=False)
    purchase_id: Mapped[int | None] = mapped_column(ForeignKey("purchases.id", ondelete="SET NULL"))
    qty: Mapped[float] = mapped_column(Float, nullable=False)          # 进货单位
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="斤")
    unit_conversion: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    qty_in_stock_unit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: 称重品的**实际净重**（库存单位）。称重品必填，不接受默认换算
    actual_net_qty: Mapped[float | None] = mapped_column(Float)
    accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class Stocktake(Base):
    """盘点单：账面 vs 实盘。

    ## M4-4 之后账面怎么来的（docs/04 §3）

    解锁 `purchases` / `receipts` 之后，系统能自己推算了：

        账面库存 = 上期实盘 + Σ入库 − Σ报损

    ⚠️ **仍缺"领用"这一项**：理论用量要 `bom`（成本卡），而 bom 按任务书 §9 仍冻结
    （M0 未通过、没有真实店配合抽盘）。所以现在的差异里仍然混着"用了没记"。
    M4 把"入库"这块补上了 —— 相比 M3 已经前进一大步（那时两项都没有），
    但**还不能说差异就等于跑冒滴漏**。bom 解锁后再收敛。
    """

    __tablename__ = "stocktakes"
    __table_args__ = (Index("ix_stocktakes_shop_date", "shop_id", "take_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    goods_id: Mapped[int] = mapped_column(ForeignKey("goods.id", ondelete="CASCADE"), nullable=False)
    take_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: 老板自报的账面数量（**可空**）。M4-4 之后系统能自己推算账面库存，
    #: 这一列退化为"老板自己记的账"，用来跟系统推算值并列对照（找出他账记错的地方）。
    book_qty: Mapped[float | None] = mapped_column(Float)
    actual_qty: Mapped[float] = mapped_column(Float, nullable=False)    # 实盘数量
    #: 系统推算的账面数量快照（上期实盘 + Σ入库 − Σ理论领用 − Σ报损，见 stock_service）。
    #: 落库是为了留痕：以后换了算法，也能看出当时的差异是拿哪个账面算的。
    computed_book_qty: Mapped[float | None] = mapped_column(Float)
    #: M5-2 盘盈盘亏超阈值（±100 元或 ±5%）时必须有审批人与原因代码
    reason_code: Mapped[str | None] = mapped_column(String(8))
    approver: Mapped[str | None] = mapped_column(String(32))
    # 主数据快照（蓝图 v1.0 §2.5 纪律五）：录单当时的价格，之后改商品单价不影响历史
    unit_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class Scrap(Base):
    """报废 / 损耗单。用途：把"差异"拆成"自己报损的"和"说不清的"。

    M5-2 加审批字段：损耗原因**必须代码化**（T01–T08）—— 自由文本不能聚合分析，等于白记。
    """

    __tablename__ = "scraps"
    __table_args__ = (Index("ix_scraps_shop_date", "shop_id", "scrap_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    goods_id: Mapped[int] = mapped_column(ForeignKey("goods.id", ondelete="CASCADE"), nullable=False)
    scrap_date: Mapped[date] = mapped_column(Date, nullable=False)
    qty: Mapped[float] = mapped_column(Float, nullable=False)
    units_lost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 疑似丢单/少收单数
    unit_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # 快照
    #: 金额快照（= qty × unit_price），写单时定死，供阈值判断与聚合直接用
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: M5-2 损耗原因代码 T01–T08（见 LOSS_REASONS）。**必填，不接受自由文本**
    reason_code: Mapped[str | None] = mapped_column(String(8))
    #: M5-2 超过阈值（±100 元或 ±5%）时**必须**有审批人，否则不许提交
    approver: Mapped[str | None] = mapped_column(String(32))
    photo_url: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


# --------------------------------------------------------------------------- #
# M5-0 · BOM / 成本卡（docs/05 §3，规格照 v1.0 §3）
#
# 授权依据：`docs/05-M5长程施工任务书.md` §3（锚点明确列出 Bom / BomVariance）
#
# ## 三条修正（v1.0 §3.2–3.4）
#   1. **出成率**：`std_qty_gross = std_qty_net / yield_rate`
#      不填出成率，标准成本必然算低 —— 采购按毛料付钱、BOM 按净料写量，毛净差是一笔永远对不上的账。
#   2. **差异拆量差/价差**：量差找厨房、价差找采购。不拆就不知道该找谁。
#   3. **ABC 抽盘**：A 每周 / B 每月 / C 每季，只盘 A 类能抓 80% 差异（否则老板第三天就放弃）。
# --------------------------------------------------------------------------- #
class Bom(Base):
    """成本卡：一道菜用多少某个料。"""

    __tablename__ = "bom"
    __table_args__ = (
        UniqueConstraint("shop_id", "dish_name", "goods_id", "version", name="uq_bom_dish_goods_ver"),
        Index("ix_bom_shop_dish", "shop_id", "dish_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    dish_name: Mapped[str] = mapped_column(String(64), nullable=False)
    goods_id: Mapped[int] = mapped_column(ForeignKey("goods.id", ondelete="CASCADE"), nullable=False)
    #: 标准**净**用量（BOM 上写的那个量）
    std_qty_net: Mapped[float] = mapped_column(Float, nullable=False)
    #: 出成率 = 加工后可用重量 ÷ 初加工前重量。1.0 = 不损耗
    yield_rate: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    #: 标准**毛**用量 = std_qty_net / yield_rate（自动算，落库是为了可追溯）
    std_qty_gross: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="斤")
    #: 标准价（元/库存单位），用于算量差与价差
    std_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    abc_class: Mapped[str] = mapped_column(String(1), nullable=False, default="C")
    #: 配方版本与生效日：改配方不能篡改历史（与"事实不可变"同一个道理）
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    effective_from: Mapped[date | None] = mapped_column(Date)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class BomVariance(Base):
    """BOM 差异：某天某道菜某个料，理论耗用 vs 实际耗用，拆成量差与价差。

    ## M5 自查第二轮 G3：一天一条，重复保存 = 覆盖

    `std_usage` 会直接减进**账面库存**（`compute_book_qty` 的 Σ理论领用）。
    如果没有唯一约束，老板手抖连点两次提交，理论领用就会翻倍 ——
    账面凭空少掉两份料，差异率虚高，**而且这个错很难被发现**。

    所以这里按 (shop_id, biz_date, dish_name, goods_id) 定唯一约束，
    路由走"覆盖"语义（与盘点单同一套，见施工版 §3.6 的口径选择）。
    """

    __tablename__ = "bom_variance"
    __table_args__ = (
        Index("ix_bom_variance_shop_date", "shop_id", "biz_date"),
        UniqueConstraint(
            "shop_id", "biz_date", "dish_name", "goods_id",
            name="uq_bom_variance_day_dish_goods",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    biz_date: Mapped[date] = mapped_column(Date, nullable=False)
    dish_name: Mapped[str] = mapped_column(String(64), nullable=False)
    goods_id: Mapped[int] = mapped_column(ForeignKey("goods.id", ondelete="CASCADE"), nullable=False)
    sold_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)      # 卖出份数
    #: 理论耗用 = sold_qty × std_qty_gross。**这一项就是"领用"** ——
    #: 任务书只授权了 bom 表（没有"领料单"表），所以领用按标准成本登记，
    #: 实盘与"入库−理论领用"的差，正好就是跑冒滴漏。
    std_usage: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: 实际耗用 = 期初 + 入库 − 期末（盘点推算），由盘点数据回填
    actual_usage: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    std_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    actual_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: 量差 = (实际用量 − 标准用量) × 标准价   ← 厨房的问题（超量、浪费、偷拿）
    qty_variance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    qty_variance_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: 价差 = (实际价 − 标准价) × 实际用量     ← 采购的问题（涨价、换供应商）
    price_variance_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    abc_class: Mapped[str] = mapped_column(String(1), nullable=False, default="C")
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


# --------------------------------------------------------------------------- #
# M5-1 · 供应商与应付账款（docs/05 §4，字段按 v0.2 §3.1）
# --------------------------------------------------------------------------- #
class Supplier(Base):
    """供应商档案。"""

    __tablename__ = "suppliers"
    __table_args__ = (Index("ix_suppliers_shop", "shop_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    main_contact: Mapped[str | None] = mapped_column(String(32))
    main_phone: Mapped[str | None] = mapped_column(String(32))
    backup_contact: Mapped[str | None] = mapped_column(String(32))
    backup_phone: Mapped[str | None] = mapped_column(String(32))
    #: 账期（天）：货到后多少天内付款
    credit_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 信用额度（元）
    credit_limit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    photo_url: Mapped[str | None] = mapped_column(Text)
    # 评级四要素的原始观测（评级 = 加权算出来的，见 suppliers 服务）
    on_time_deliveries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_deliveries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    qualified_batches: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_batches: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 配送时效（下单到到货的平均天数 ×100 存整数，避免浮点误差堆积）
    delivery_lead_days: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)


class Payable(Base):
    """应付账款：一笔采购欠谁多少、什么时候到期、付了多少。

    **为什么单独建表而不是记进 `purchases`**：一笔付款可以覆盖多笔采购
    （老板常见做法是月底一起结），记进采购单就表达不了这种核销关系。
    """

    __tablename__ = "payables"
    __table_args__ = (
        Index("ix_payables_shop_due", "shop_id", "due_date"),
        Index("ix_payables_supplier", "supplier_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False
    )
    purchase_id: Mapped[int | None] = mapped_column(ForeignKey("purchases.id", ondelete="SET NULL"))
    biz_date: Mapped[date] = mapped_column(Date, nullable=False)      # 采购日期
    due_date: Mapped[date] = mapped_column(Date, nullable=False)      # 到期日 = 采购日 + 账期
    #: 应付金额（原价 × 数量）
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: 折价数量与折价金额（货不对板时供应商让掉的）
    discount_qty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    discount_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: 实付
    paid_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    note: Mapped[str | None] = mapped_column(Text)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)

    @property
    def payable_amount(self) -> float:
        """应结金额 = 应付 − 折价金额。"""
        return round(float(self.amount or 0.0) - float(self.discount_amount or 0.0), 6)

    @property
    def outstanding(self) -> float:
        """待付 = 应付 − 折价 − 已付。"""
        return round(self.payable_amount - float(self.paid_amount or 0.0), 6)
