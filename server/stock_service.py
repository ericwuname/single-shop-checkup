"""账面库存推算与单据写入（M4-4）。

## 账面库存怎么来的

    账面库存 = 上期实盘 + Σ入库 − Σ报损

* **上期实盘**：该商品在本次盘点之前最近一次盘点的实盘数（没有就从 0 起算）
* **Σ入库**：区间内的 `receipts.qty_in_stock_unit` 之和（已按换算率折成库存单位）
* **Σ报损**：区间内的 `scraps.qty` 之和

## ⚠️ 仍然缺"领用"这一项

出库（后厨领用）需要 `bom`（成本卡里的理论用量），而 bom 按 docs/04 §9 仍冻结
（M0 未通过、没有真实店配合抽盘）。所以现在的差异里仍然混着"用了没记"。

相比 M3 已经前进一大步 —— 那时"入库"和"出库"两项都没有，账面只能靠老板手填；
现在入库是真实单据了。**但还不能说差异就等于跑冒滴漏。**

本模块是**账面库存的唯一实现**：引擎注入（metric）与报告明细（清单）都调它，
不许各写一份 —— 两份实现必然漂移。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .models import Goods, Purchase, Receipt, Scrap, Stocktake


def previous_stocktake(
    session: Session, shop_id: int, goods_id: int, before: date
) -> Stocktake | None:
    """本次盘点之前**最近一次**盘点单（不含当天，避免自己和自己比）。"""
    return session.scalars(
        select(Stocktake)
        .where(
            Stocktake.shop_id == shop_id,
            Stocktake.goods_id == goods_id,
            Stocktake.take_date < before,
        )
        .order_by(Stocktake.take_date.desc(), Stocktake.id.desc())
    ).first()


def sum_receipts(session: Session, shop_id: int, goods_id: int, start: date, end: date) -> float:
    total = session.scalar(
        select(func.coalesce(func.sum(Receipt.qty_in_stock_unit), 0.0)).where(
            Receipt.shop_id == shop_id,
            Receipt.goods_id == goods_id,
            Receipt.receipt_date >= start,
            Receipt.receipt_date <= end,
            Receipt.accepted == 1,
        )
    )
    return float(total or 0.0)


def sum_scraps(session: Session, shop_id: int, goods_id: int, start: date, end: date) -> float:
    total = session.scalar(
        select(func.coalesce(func.sum(Scrap.qty), 0.0)).where(
            Scrap.shop_id == shop_id,
            Scrap.goods_id == goods_id,
            Scrap.scrap_date >= start,
            Scrap.scrap_date <= end,
        )
    )
    return float(total or 0.0)


def _book_from_parts(
    base: float,
    base_from: str,
    has_base: bool,
    inbound: float,
    outbound: float,
    scrapped: float,
) -> tuple[float, dict[str, Any]]:
    """**账面公式的唯一实现**（M5 自查第二轮 E5 拆出来的）。

    单点版（`compute_book_qty`）和区间递推版（`book_qty_series`）都调它 ——
    不许有两份"怎么算"的逻辑，否则必然漂移。
    """
    book = base + inbound - outbound - scrapped
    return book, {
        "base_qty": base,
        "base_from": base_from,
        # 期初建账（没有上期实盘）时账面从 0 起算，"差异"其实就是全部库存 ——
        # 那不是盈亏，不能拿去要审批、要原因代码。调用方靠这个标志区分。
        "has_base": has_base,
        "inbound_qty": round(inbound, 4),
        "theoretical_usage_qty": round(outbound, 4),
        "scrap_qty": round(scrapped, 4),
        "formula": "上期实盘 + Σ入库 − Σ理论领用 − Σ报损",
        "missing": None,
        "note": (
            "领用按 BOM 的标准用量登记（卖出份数 × 标准毛料用量），"
            "所以这里的差异是「实际用掉的 − 应该用掉的」，也就是跑冒滴漏。"
        ),
    }


def compute_book_qty(
    session: Session, shop_id: int, goods_id: int, as_of: date
) -> tuple[float, dict[str, Any]]:
    """算到 `as_of` 为止的账面库存（库存单位）。返回 (数量, 推理过程)。

    M5-0 起公式补上**领用**：

        账面 = 上期实盘 + Σ入库 − Σ理论领用 − Σ报损

    `Σ理论领用` 来自 BOM（卖出份数 × 标准毛料用量），见 `bom_service`。
    补上这一项之后，实盘与账面的差才等于**跑冒滴漏** ——
    在此之前"正常用掉的料"也被算进差异里，数字虚高且指错方向。

    推理过程返回出来是为了**可解释**：老板问"这个账面哪来的"，
    我们要能一条条列给他看，而不是给一个黑箱数字。
    """
    from .bom_service import usage_by_goods

    prev = previous_stocktake(session, shop_id, goods_id, as_of)
    if prev is not None:
        base = float(prev.actual_qty)
        start = prev.take_date
        base_from = f"{start.isoformat()} 实盘"
    else:
        base = 0.0
        start = date(1900, 1, 1)
        base_from = "无历史盘点，从 0 起算"

    inbound = sum_receipts(session, shop_id, goods_id, start, as_of)
    scrapped = sum_scraps(session, shop_id, goods_id, start, as_of)
    outbound = usage_by_goods(session, shop_id, goods_id, start, as_of)
    return _book_from_parts(base, base_from, prev is not None, inbound, outbound, scrapped)


def book_qty_series(
    session: Session, shop_id: int, goods_id: int, start: date, end: date
) -> dict[date, tuple[float, dict[str, Any]]]:
    """一次算出一段时间内**每次盘点**的账面值（M5 自查第二轮 E5）。

    ## 为什么要有它

    `variance_detail` / `stock_amounts` 原来对每张盘点单调一次 `compute_book_qty`，
    每张单 4 次查询。一个月 150 张盘点 = 600 次数据库往返 ——
    报告页是老板**最常看**的页面，手机上就是几百毫秒到几秒的等待。

    ## 为什么不会变成"第二份实现"

    公式仍然是 `_book_from_parts`（唯一实现），这里只是**一次取数、递推复用**：

        每张盘点单的账面 = 上一张的实盘 + 两者之间入库 − 两者之间领用 − 两者之间报损

    递推和逐张单算在数学上等价（都是"上期实盘 + 区间增量"），
    但有测试钉住这一点（`test_book_qty_series_matches_pointwise`）——
    **等价性是断言出来的，不是我嘴说的**。
    """
    from .bom_service import BomVariance

    rows = session.scalars(
        select(Stocktake)
        .where(
            Stocktake.shop_id == shop_id,
            Stocktake.goods_id == goods_id,
            Stocktake.take_date >= start,
            Stocktake.take_date <= end,
        )
        .order_by(Stocktake.take_date, Stocktake.id)
    ).all()
    if not rows:
        return {}

    # 一次性取数：区间内的入库 / 报损 / 理论领用，按日期预聚合
    receipts = _daily_sums(session, Receipt, shop_id, goods_id, "receipt_date",
                           "qty_in_stock_unit", start, end)
    scraps = _daily_sums(session, Scrap, shop_id, goods_id, "scrap_date", "qty", start, end)
    # ⚠️ 理论领用取的是 `std_usage`（= 卖出份数 × 标准毛料用量），
    # 不是"实际用量"—— 账面要扣的是"应该用掉多少"，不是"实际用掉多少"。
    usage = _daily_sums(session, BomVariance, shop_id, goods_id, "biz_date",
                        "std_usage", start, end)

    out: dict[date, tuple[float, dict[str, Any]]] = {}
    base = float(rows[0].actual_qty) if rows else 0.0
    # 第一张的账面：它自己的"上期"要单独查一次（区间外的那次）
    prev = previous_stocktake(session, shop_id, goods_id, rows[0].take_date)
    if prev is not None:
        base = float(prev.actual_qty)
        base_from = f"{prev.take_date.isoformat()} 实盘"
        has_base = True
        first_start = prev.take_date
    else:
        base = 0.0
        base_from = "无历史盘点，从 0 起算"
        has_base = False
        first_start = date(1900, 1, 1)

    cursor = first_start
    for row in rows:
        seg_receipts = _sum_between(receipts, cursor, row.take_date)
        seg_scraps = _sum_between(scraps, cursor, row.take_date)
        seg_usage = _sum_between(usage, cursor, row.take_date)
        book, why = _book_from_parts(
            base, base_from, has_base, seg_receipts, seg_usage, seg_scraps
        )
        out[row.take_date] = (book, why)
        # 递推：下一张的期初 = 这一张的实盘
        base = float(row.actual_qty)
        base_from = f"{row.take_date.isoformat()} 实盘"
        has_base = True
        cursor = row.take_date
    return out


def _daily_sums(
    session, model, shop_id: int, goods_id: int, date_col: str, qty_col: str,
    start: date, end: date,
) -> dict[date, float]:
    """把某类单据按日期汇总成 {日期: 合计}，一次查询拿完整个区间。

    `qty_col` 要显式传：三类单据的数量列名不一样
    （receipts 用 `qty_in_stock_unit`、scraps 用 `qty`、bom_variance 用 `std_usage`）。
    写错列名不会报错，只会算出错数 —— 所以这里不猜，一律显式。
    """
    col = getattr(model, date_col)
    qty = getattr(model, qty_col)
    rows = session.execute(
        select(col, func.sum(qty))
        .where(
            model.shop_id == shop_id,
            model.goods_id == goods_id,
            col >= start,
            col <= end,
        )
        .group_by(col)
    ).all()
    return {d: float(v or 0.0) for d, v in rows}


def _sum_between(daily: dict[date, float], start: date, end: date) -> float:
    """区间合计（**含 start、含 end**）。

    ⚠️ 含 start 是必须的：逐张单算的 `sum_receipts(start, as_of)` 用的是 `>= start`，
    如果这里改成 `> start`，一旦入库日正好等于上一次盘点日就会漏掉那一笔 ——
    边界不一致会让"递推"和"逐张"算出不同的账面（E5 的等价性测试就是抓这个的）。
    """
    total = 0.0
    for d, v in daily.items():
        if start <= d <= end:
            total += v
    return total


def explained_loss_amount(
    session: Session,
    shop_id: int,
    start: date,
    end: date,
    *,
    needs_approval=None,
) -> float:
    """区间内**已登记且通过审核**的损耗金额（M6，docs/06 §0 裁定 7）。

    行业指标「可解释损耗率 = 已登记且通过审核的损耗金额 ÷ 总损耗金额」的分子。
    配上 `scrap_amount` 当分母，老板看到的是：

        损耗 8,000 元，其中 6,500 元有登记原因、1,500 元说不清

    而不是干巴巴一个 8,000 —— **能追责的才是能改进的**。

    ## 判定口径（两条同时满足）

    1. **已登记**：`reason_code` 非空（T01–T08，M5-2 起必填）
    2. **通过审核**：这笔如果**触发过审批阈值**，就必须带 `approver`

    第 2 条是关键：不加上它，"已解释"可以自填自审 ——
    老板自己录一笔 2000 元报废、自己填自己当审批人，指标就假了。

    ⚠️ **阈值判定必须和审批路由用同一个 base**：报废的比例分母是
    **当月该货的采购额**（`routers/stock.py::create_scrap` 的口径）。
    早先这里图省事传了 `None`，结果比例永远算不出来、"通过审核"成了死条件 ——
    M6 自查抓到的，已改。

    `needs_approval` 可注入，避免和 approvals 模块循环 import。
    """
    if needs_approval is None:
        from .approvals import needs_approval as _na

        needs_approval = _na

    rows = session.scalars(
        select(Scrap).where(
            Scrap.shop_id == shop_id,
            Scrap.scrap_date >= start,
            Scrap.scrap_date <= end,
        )
    ).all()

    # 比例分母：区间内每个货的采购额（与审批路由同口径，不另写一套）
    purchase_by_goods: dict[int, float] = {}
    if rows:
        goods_ids = {r.goods_id for r in rows}
        purchase_rows = session.execute(
            select(Purchase.goods_id, func.coalesce(func.sum(Purchase.amount), 0.0))
            .where(
                Purchase.shop_id == shop_id,
                Purchase.goods_id.in_(goods_ids),
                Purchase.purchase_date >= start,
                Purchase.purchase_date <= end,
            )
            .group_by(Purchase.goods_id)
        ).all()
        purchase_by_goods = {gid: float(v or 0.0) for gid, v in purchase_rows}

    total = 0.0
    for row in rows:
        if not row.reason_code:
            continue                       # 没登记原因 → 说不清
        amount = (
            float(row.amount)
            if row.amount and row.amount > 0
            else round(float(row.qty or 0.0) * float(row.unit_price or 0.0), 6)
        )
        need, _why = needs_approval(amount, purchase_by_goods.get(row.goods_id) or None)
        if need and not (row.approver or "").strip():
            continue                       # 该签字没签 → 不算已解释
        total += amount
    return round(total, 6)


def stock_amounts(
    session: Session, shop_id: int, start: date, end: date
) -> dict[str, float | None]:
    """给引擎注入用的三个金额：账面 / 实盘 / 报损。

    区间口径：只算 `[start, end]` 里的盘点单，每张的账面优先用系统推算值，
    推算不出才回退到老板自报的 `book_qty`。

    ⚠️ **报损金额与盘点单无关**：老板可能只记了报损、还没盘点过。
    早先的写法是"没有盘点单就三个一起返回 None"，结果报了损也看不见 ——
    报损是有独立意义的（它解释差异里"有多少是你自己知道的"），不该被盘点单绑架。

    M6 追加 `explained`（docs/06 §0 裁定 7，可解释损耗率的分子）：
    **已登记且通过审核**的损耗金额。判定口径见 `explained_loss_amount`。
    """
    from .approvals import needs_approval

    # M5-2：报废金额以**写单时定死的快照** `amount` 为准（阈值判断与聚合都用同一个数，
    # 不能一个地方按快照算、另一个地方按当前单价算）。老数据没写 amount 才回退 qty×price。
    scrap_total = session.scalar(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (Scrap.amount > 0, Scrap.amount),
                        else_=Scrap.qty * Scrap.unit_price,
                    )
                ),
                0.0,
            )
        ).where(
            Scrap.shop_id == shop_id,
            Scrap.scrap_date >= start,
            Scrap.scrap_date <= end,
        )
    )
    scrap_amount = round(float(scrap_total or 0.0), 6)

    explained = explained_loss_amount(
        session, shop_id, start, end, needs_approval=needs_approval
    )

    rows = session.scalars(
        select(Stocktake)
        .where(
            Stocktake.shop_id == shop_id,
            Stocktake.take_date >= start,
            Stocktake.take_date <= end,
        )
        .order_by(Stocktake.take_date, Stocktake.id)
    ).all()
    if not rows:
        # 没盘点过 → 账面/实盘是 None（不是 0）：不给"差异 0%"这种假安心的话
        return {"book": None, "actual": None, "scrap": scrap_amount, "explained": explained}

    book_amount = 0.0
    actual_amount = 0.0
    counted = 0
    # E5：同 variance_detail —— 一次算完整段，不逐张单查库
    by_goods: dict[int, list[Stocktake]] = {}
    for row in rows:
        by_goods.setdefault(row.goods_id, []).append(row)
    series_cache: dict[int, dict[date, tuple[float, dict[str, Any]]]] = {}
    for goods_id, goods_rows in by_goods.items():
        series_cache[goods_id] = book_qty_series(
            session, shop_id, goods_id, goods_rows[0].take_date, goods_rows[-1].take_date
        )

    for row in rows:
        price = float(row.unit_price or 0.0)
        series = series_cache.get(row.goods_id) or {}
        computed = series[row.take_date][0] if row.take_date in series else None
        if computed is None:
            computed, _why = compute_book_qty(session, row.shop_id, row.goods_id, row.take_date)
        book_qty = effective_book_qty(session, row, computed)
        # ⚠️ M5 修的一处口径缺陷：账面算不出来的那张盘点单（通常是**期初建账**）
        # 原来会被当成"账面 0"计进分母，把差异率顶得虚高（实测 20% → 46.7%）。
        # 建账不是盈亏，没有可比账面就不该参与差异统计。
        if book_qty is None:
            continue
        counted += 1
        actual_amount += float(row.actual_qty) * price
        book_amount += book_qty * price

    if counted == 0:
        # 全是建账单 → 账面/实盘仍是 None（不给"差异 0%"这种假安心的话）
        return {"book": None, "actual": None, "scrap": scrap_amount, "explained": explained}

    return {
        "book": round(book_amount, 6),
        "actual": round(actual_amount, 6),
        "scrap": scrap_amount,
        "explained": explained,
    }


def effective_book_qty(
    session: Session, row: Stocktake, computed: float | None = None
) -> float | None:
    """一张盘点单该用哪个账面数：**系统推算优先**，老板自报兜底。

    M4-4 的目标是"不再靠手填"。但如果系统完全推不出来（比如这张盘点单之前
    既没有历史盘点、也没有任何入库单），硬要给 0 就等于说"账面应该是 0"，
    那是拿一个假的账面去比实盘 —— 所以这时退回老板自报值（他可能确实记了账）。

    ⚠️ **这是"用哪个账面"的唯一实现**。审批判定、差异报表、指标引擎都必须调它。
    M5 自查发现路由里曾另写一份"老板自报优先"的逻辑（审查项 C2/E2），
    结果同一张盘点单在"签字时"和"看报告时"是两个不同的差异 —— 已收敛到这一处。
    `computed` 可传入以免重复计算（调用方常常已经算过一次）。
    """
    if computed is None:
        computed, _why = compute_book_qty(session, row.shop_id, row.goods_id, row.take_date)
    has_basis = computed > 0 or previous_stocktake(session, row.shop_id, row.goods_id, row.take_date)
    if has_basis:
        return round(computed, 6)
    if row.book_qty is not None:
        return float(row.book_qty)
    return None


def variance_detail(session: Session, shop_id: int, start: date, end: date) -> list[dict[str, Any]]:
    """逐货品的差异明细（含账面来源说明）——报告与 /api/stock/variance 共用。"""
    rows = session.scalars(
        select(Stocktake)
        .where(
            Stocktake.shop_id == shop_id,
            Stocktake.take_date >= start,
            Stocktake.take_date <= end,
        )
        .order_by(Stocktake.take_date, Stocktake.id)
    ).all()
    goods = {
        g.id: g for g in session.scalars(select(Goods).where(Goods.shop_id == shop_id)).all()
    }

    items: list[dict[str, Any]] = []
    # E5：按货品分组，一次算完整段区间的账面（递推），而不是每张单都查一遍库。
    by_goods: dict[int, list[Stocktake]] = {}
    for row in rows:
        by_goods.setdefault(row.goods_id, []).append(row)

    series_cache: dict[int, dict[date, tuple[float, dict[str, Any]]]] = {}
    for goods_id, goods_rows in by_goods.items():
        series_cache[goods_id] = book_qty_series(
            session, shop_id, goods_id, goods_rows[0].take_date, goods_rows[-1].take_date
        )

    for row in rows:
        item = goods.get(row.goods_id)
        series = series_cache.get(row.goods_id) or {}
        if row.take_date in series:
            _computed, why = series[row.take_date]
        else:
            _computed, why = compute_book_qty(session, row.shop_id, row.goods_id, row.take_date)
        book = effective_book_qty(session, row, _computed)
        actual = float(row.actual_qty)
        price = float(row.unit_price or 0.0)
        diff = actual - (book or 0.0)
        items.append(
            {
                "goods_id": row.goods_id,
                "goods_name": item.name if item else str(row.goods_id),
                "spec": item.spec if item else None,
                "code": item.code if item else None,
                "unit": (item.unit if item else "") or "",
                "take_date": row.take_date.isoformat(),
                "book_qty": book,
                "book_qty_source": "系统推算" if book is not None else None,
                "book_reason": why,
                "reported_book_qty": row.book_qty,
                "actual_qty": actual,
                "variance_qty": round(diff, 4),
                "unit_price": price,
                "variance_amount": round(diff * price, 2),
                "variance_rate": (abs(diff) / book) if book else None,
                "scrap_qty": why["scrap_qty"],
            }
        )
    return items
