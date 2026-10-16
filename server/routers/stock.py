"""商品主数据 + 库存单据 + 账实差异（M3-3 起建，M4 扩充）。

授权依据：`docs/03` §3 M3-3、`docs/04` §2 M4-3 / §2bis M4-10~12 / §3 M4-4。

本文件负责三块：
  1. **分类树**（M4-3）：预置 9 + 44，支持老板加二级
  2. **商品主数据**（M4-10/11/12）：规格 / 三单位换算 / 查重 / 合并 / 停用
  3. **单据**（M3-3 + M4-4）：采购单、入库验收单、盘点单、报废单 → 账面库存可推算

两条红线（写在代码里，不靠自觉）：
  * **编码被单据引用过 → 拒绝修改**，只能停用后新建（`goods_master.block_code_change`）
  * **称重品入库 → 不填实际净重不许提交**（包装上那个固定重量不是实重）
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import approvals
from .. import goods_master as gm
from ..db import get_db
from ..models import (
    Goods,
    GoodsAlias,
    GoodsCategory,
    Payable,
    Purchase,
    Receipt,
    Scrap,
    Stocktake,
    Supplier,
)
from ..schemas import (
    AliasIn,
    AliasOut,
    BulkResult,
    CategoryCreate,
    CategoryOut,
    DuplicateCheckOut,
    GoodsCreate,
    GoodsMergeIn,
    GoodsMergeOut,
    GoodsOut,
    GoodsUpdate,
    LossReasonBreakdown,
    LossReasonOut,
    PurchaseIn,
    PurchaseOut,
    ReceiptOut,
    ScrapIn,
    ScrapOut,
    StocktakeIn,
    StocktakeOut,
)
from ..stock_service import variance_detail
from .daily import require_shop
from .report import _stock_variance

router = APIRouter(tags=["stock"])


# =========================================================================== #
# M4-3 · 分类树
# =========================================================================== #
@router.get("/api/categories", response_model=list[CategoryOut], summary="分类树（9 一级 + 44 二级）")
def list_categories(
    level: int | None = Query(default=None, ge=1, le=2),
    used_only: bool = Query(default=False, description="只返回被商品用过的分类（下拉不该铺开 44 个）"),
    shop_id: int | None = Query(default=None, description="配合 used_only 用"),
    session: Session = Depends(get_db),
) -> list[GoodsCategory]:
    stmt = select(GoodsCategory).where(GoodsCategory.enabled == 1)
    if level is not None:
        stmt = stmt.where(GoodsCategory.level == level)
    rows = list(session.scalars(stmt.order_by(GoodsCategory.sort_order)).all())

    if used_only and shop_id is not None:
        used = {
            code
            for (code,) in session.execute(
                select(Goods.category_code).where(
                    Goods.shop_id == shop_id, Goods.category_code.is_not(None)
                )
            ).all()
        }
        parents = {c.split("-")[0] for c in used}
        rows = [r for r in rows if r.code in used or r.code in parents]
    return rows


@router.post("/api/categories", response_model=CategoryOut, status_code=201, summary="加一个自定义二级分类")
def create_category(payload: CategoryCreate, session: Session = Depends(get_db)) -> GoodsCategory:
    parent = session.get(GoodsCategory, payload.parent_code)
    if parent is None or parent.level != 1:
        raise HTTPException(status_code=400, detail=f"一级分类 {payload.parent_code!r} 不存在")
    siblings = session.scalars(
        select(GoodsCategory).where(GoodsCategory.parent_code == payload.parent_code)
    ).all()
    nxt = max([int(s.code.split("-")[1]) for s in siblings] + [0]) + 1
    row = GoodsCategory(
        code=f"{payload.parent_code}-{nxt:02d}",
        parent_code=payload.parent_code,
        name=payload.name,
        level=2,
        sort_order=parent.sort_order * 100 + nxt,
        is_custom=1,
        enabled=1,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


# =========================================================================== #
# M4-10 / M4-11 / M4-12 · 商品主数据
# =========================================================================== #
def _resolve_category_path(session: Session, code: str) -> str:
    parent = session.get(GoodsCategory, code)
    if parent is None or parent.level != 2:
        raise HTTPException(
            status_code=400,
            detail=f"分类 {code!r} 不存在或不是二级分类（商品必须挂二级；本产品只拆两级）",
        )
    top = session.get(GoodsCategory, parent.parent_code) if parent.parent_code else None
    return " / ".join(p for p in [top.name if top else None, parent.name] if p)


@router.get("/api/goods", response_model=list[GoodsOut], summary="商品列表（支持拼音首字母检索）")
def list_goods(
    shop_id: int = Query(...),
    q: str | None = Query(default=None, description="名称/规格/编码/拼音首字母/别名"),
    category_code: str | None = Query(default=None),
    enabled_only: bool = Query(default=True),
    include_merged: bool = Query(default=False, description="是否包含已合并的副码"),
    session: Session = Depends(get_db),
) -> list[Goods]:
    require_shop(session, shop_id)
    if q:
        rows = gm.search_goods(session, shop_id, q)
    else:
        stmt = select(Goods).where(Goods.shop_id == shop_id)
        if enabled_only:
            stmt = stmt.where(Goods.enabled == 1)
        rows = list(session.scalars(stmt.order_by(Goods.code)).all())

    if category_code:
        rows = [r for r in rows if (r.category_code or "").startswith(category_code)]
    if not include_merged:
        rows = [r for r in rows if r.merged_into_id is None]
    return rows


@router.post("/api/goods/check-duplicates", response_model=DuplicateCheckOut, summary="查重预览（三步法第 1 步）")
def check_duplicates(
    shop_id: int = Query(...),
    name: str = Query(..., min_length=1),
    spec: str | None = Query(default=None),
    threshold: float = Query(default=gm.DEFAULT_DUPLICATE_THRESHOLD, ge=0.0, le=1.0),
    session: Session = Depends(get_db),
) -> DuplicateCheckOut:
    require_shop(session, shop_id)
    candidates, must_confirm = gm.check_duplicates(
        session, shop_id=shop_id, name=name, spec=spec, threshold=threshold
    )
    return DuplicateCheckOut(candidates=candidates, threshold=threshold, must_confirm=must_confirm)


@router.post("/api/goods", response_model=GoodsOut, status_code=201, summary="新建商品（创建前强制查重）")
def create_goods(payload: GoodsCreate, session: Session = Depends(get_db)) -> Goods:
    shop = require_shop(session, payload.shop_id)
    path = _resolve_category_path(session, payload.category_code)

    # 三步法第 3 步：创建前强制查重。同名同规格 → 必须人工点过"确认不是重复"
    candidates, must_confirm = gm.check_duplicates(
        session, shop_id=shop.id, name=payload.name, spec=payload.spec
    )
    if must_confirm and not payload.confirmed_not_duplicate:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "发现同名同规格的已有物料，请确认不是重复",
                "candidates": candidates,
                "hint": "复用已有的 → 直接用候选里的 goods_id；确认新建 → 带 confirmed_not_duplicate=true",
            },
        )

    code = payload.code or gm.next_code(
        session, shop.id, payload.category_code, payload.spec_code or "01"
    )
    if session.scalars(select(Goods).where(Goods.shop_id == shop.id, Goods.code == code)).first():
        raise HTTPException(status_code=409, detail=f"商品编号 {code!r} 已存在")

    data = payload.model_dump(exclude={"confirmed_not_duplicate", "code"})
    row = Goods(code=code, category_path=path, **data)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.patch("/api/goods/{goods_id}", response_model=GoodsOut, summary="改商品（编码被引用则拒绝）")
def update_goods(
    goods_id: int,
    payload: GoodsUpdate,
    code: str | None = Query(default=None, description="⚠ 被单据引用过的商品改编码会被拒绝"),
    session: Session = Depends(get_db),
) -> Goods:
    row = session.get(Goods, goods_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"商品 {goods_id} 不存在")

    changes = payload.model_dump(exclude_unset=True)
    # 🔴 红线：编码被单据引用过就永不修改（payload 与 query 两条路都要拦，
    # 否则改编码会变成"无声的空操作"，比报错更糟）
    desired_code = changes.pop("code", None) or code
    if desired_code is not None:
        try:
            gm.block_code_change(session, row, desired_code)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        row.code = desired_code

    if changes.get("category_code"):
        changes["category_path"] = _resolve_category_path(session, changes["category_code"])

    # 换算系数变更留痕（供应商改包装是高频事件，改了不记就对不上账）
    if "unit_conversion" in changes and float(changes["unit_conversion"]) != float(
        row.unit_conversion
    ):
        from datetime import datetime as _dt

        note = changes.pop("unit_conversion_change_note", None)
        row.unit_conversion_changed_at = _dt.now()
        row.unit_conversion_change_note = (
            note or f"由 {row.unit_conversion:g} 改为 {float(changes['unit_conversion']):g}"
        )
    else:
        changes.pop("unit_conversion_change_note", None)

    for key, value in changes.items():
        setattr(row, key, value)
    session.commit()
    session.refresh(row)
    return row


@router.get("/api/goods/{goods_id}", response_model=GoodsOut, summary="商品详情")
def get_goods(goods_id: int, session: Session = Depends(get_db)) -> Goods:
    row = session.get(Goods, goods_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"商品 {goods_id} 不存在")
    return row


@router.post("/api/goods/merge", response_model=GoodsMergeOut, summary="合并重复码（三步法第 2 步）")
def merge_goods(payload: GoodsMergeIn, session: Session = Depends(get_db)) -> GoodsMergeOut:
    target = session.get(Goods, payload.target_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"主码 {payload.target_id} 不存在")
    try:
        result = gm.merge_goods(
            session,
            shop_id=target.shop_id,
            source_ids=payload.source_ids,
            target_id=payload.target_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return GoodsMergeOut(**result)


@router.get("/api/goods/{goods_id}/aliases", response_model=list[AliasOut], summary="别名列表")
def list_aliases(goods_id: int, session: Session = Depends(get_db)) -> list[GoodsAlias]:
    return list(session.scalars(select(GoodsAlias).where(GoodsAlias.goods_id == goods_id)).all())


@router.post("/api/goods/{goods_id}/aliases", response_model=AliasOut, status_code=201, summary="加别名（异名同物）")
def add_alias(goods_id: int, payload: AliasIn, session: Session = Depends(get_db)) -> GoodsAlias:
    row = session.get(Goods, goods_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"商品 {goods_id} 不存在")
    exists = session.scalars(
        select(GoodsAlias).where(
            GoodsAlias.shop_id == row.shop_id, GoodsAlias.alias == payload.alias
        )
    ).first()
    if exists is not None:
        raise HTTPException(status_code=409, detail=f"别名 {payload.alias!r} 已挂到商品 {exists.goods_id}")
    alias = GoodsAlias(shop_id=row.shop_id, goods_id=goods_id, alias=payload.alias, note=payload.note)
    session.add(alias)
    session.commit()
    session.refresh(alias)
    return alias


# =========================================================================== #
# M4-4 · 采购单 / 入库验收单
# =========================================================================== #
def _write_purchase(session: Session, payload: PurchaseIn) -> Purchase:
    goods = session.get(Goods, payload.goods_id)
    if goods is None:
        raise HTTPException(status_code=404, detail=f"商品 {payload.goods_id} 不存在")
    unit = payload.unit or goods.purchase_unit or goods.unit
    conv = float(goods.unit_conversion or 1.0)
    price = (
        float(payload.unit_price)
        if payload.unit_price is not None
        else round(float(goods.unit_price) * conv, 6)
    )
    amount = round(payload.qty * price, 6)

    # M5-2：折价超阈值（±100 元或 ±5%）必须有人签字 —— 没签字的账最后没人认
    discount_amount = float(payload.discount_amount or 0.0)
    if discount_amount > 0:
        try:
            approvals.require_approval(
                amount=discount_amount,
                base=amount,
                approver=payload.approver,
                what=f"这笔折让 {discount_amount:,.0f} 元",
            )
        except approvals.ApprovalRequiredError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    supplier = None
    if payload.supplier_id is not None:
        supplier = session.get(Supplier, payload.supplier_id)
        if supplier is None:
            raise HTTPException(status_code=404, detail=f"供应商 {payload.supplier_id} 不存在")

    row = Purchase(
        shop_id=payload.shop_id,
        goods_id=goods.id,
        purchase_date=payload.purchase_date,
        qty=payload.qty,
        unit=unit,
        unit_conversion=conv,              # 主数据快照：改商品不影响历史单据
        unit_price=price,
        qty_in_stock_unit=round(payload.qty * conv, 6),
        amount=amount,
        category_code=goods.category_code,
        supplier_id=payload.supplier_id,
        discount_qty=float(payload.discount_qty or 0.0),
        discount_amount=discount_amount,
        approver=payload.approver,
        note=payload.note,
    )
    session.add(row)
    gm.mark_referenced(session, goods.id)   # 引用过一次 → 编码永远锁住
    session.flush()

    # M5-1：挂账供应商（credit_days > 0）自动生成应付，不用老板再记一遍
    if supplier is not None and int(supplier.credit_days or 0) > 0:
        session.add(
            Payable(
                shop_id=payload.shop_id,
                supplier_id=supplier.id,
                purchase_id=row.id,
                biz_date=payload.purchase_date,
                due_date=payload.purchase_date + timedelta(days=int(supplier.credit_days)),
                amount=round(amount - discount_amount, 6),
                discount_qty=float(payload.discount_qty or 0.0),
                discount_amount=discount_amount,
            )
        )
        session.flush()
    return row


def _purchase_out(session: Session, row: Purchase) -> PurchaseOut:
    out = PurchaseOut.model_validate(row)
    if row.supplier_id is not None:
        supplier = session.get(Supplier, row.supplier_id)
        out.supplier_name = supplier.name if supplier else None
    return out


@router.post("/api/purchases", response_model=PurchaseOut, status_code=201, summary="记一笔采购")
def create_purchase(payload: PurchaseIn, session: Session = Depends(get_db)) -> PurchaseOut:
    require_shop(session, payload.shop_id)
    row = _write_purchase(session, payload)
    session.commit()
    session.refresh(row)
    return _purchase_out(session, row)


@router.post("/api/purchases/bulk", response_model=BulkResult, summary="批量记采购（数据灌入用）")
def create_purchases_bulk(
    shop_id: int = Query(...),
    items: list[PurchaseIn] = Body(...),
    session: Session = Depends(get_db),
) -> BulkResult:
    """三年数据要灌 1 万+ 条采购，逐条 HTTP 跑不完（实测每条 0.2 秒 = 半小时以上）。

    逐条仍然做同样的校验与快照，只是共用一次事务。
    """
    require_shop(session, shop_id)
    result = BulkResult()
    for item in items:
        try:
            if item.shop_id != shop_id:
                raise ValueError("shop_id 与路径参数不一致")
            _write_purchase(session, item)
            result.inserted += 1
        except Exception as exc:                     # noqa: BLE001 - 批量要收集错误而不是中断
            result.errors.append(f"{item.purchase_date} goods={item.goods_id}: {exc}")
    session.commit()
    return result


@router.get("/api/purchases", response_model=list[PurchaseOut], summary="按区间列出采购单")
def list_purchases(
    shop_id: int = Query(...),
    start: date = Query(...),
    end: date = Query(...),
    category_code: str | None = Query(default=None),
    session: Session = Depends(get_db),
) -> list[PurchaseOut]:
    require_shop(session, shop_id)
    if start > end:
        start, end = end, start
    rows = list(
        session.scalars(
            select(Purchase)
            .where(
                Purchase.shop_id == shop_id,
                Purchase.purchase_date >= start,
                Purchase.purchase_date <= end,
            )
            .order_by(Purchase.purchase_date, Purchase.id)
        ).all()
    )
    if category_code:
        rows = [r for r in rows if (r.category_code or "").startswith(category_code)]
    return [_purchase_out(session, r) for r in rows]


def _write_receipt(session: Session, payload: PurchaseIn) -> Receipt:
    goods = session.get(Goods, payload.goods_id)
    if goods is None:
        raise HTTPException(status_code=404, detail=f"商品 {payload.goods_id} 不存在")

    # 🔴 红线：称重品不填实际净重不许提交
    # （"把包装上的固定重量当实际重量"是库存差异的头号错因）
    if goods.is_weighed and payload.actual_net_qty is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{goods.name}（{goods.spec or ''}）是称重品，入库必须录**实际净重** —— "
                f"包装上那个固定重量不是实重"
            ),
        )

    unit = payload.unit or goods.purchase_unit or goods.unit
    conv = float(goods.unit_conversion or 1.0)
    price = (
        float(payload.unit_price)
        if payload.unit_price is not None
        else round(float(goods.unit_price) * conv, 6)
    )
    # 称重品以**实重**作为入库数量（库存单位）；非称重品用 数量 × 换算率
    qty_stock = float(payload.actual_net_qty) if goods.is_weighed else round(payload.qty * conv, 6)
    row = Receipt(
        shop_id=payload.shop_id,
        goods_id=goods.id,
        receipt_date=payload.purchase_date,
        qty=payload.qty,
        unit=unit,
        unit_conversion=conv,
        qty_in_stock_unit=qty_stock,
        unit_price=price,
        amount=round(payload.qty * price, 6),
        actual_net_qty=payload.actual_net_qty,
        accepted=1,
        note=payload.note,
    )
    session.add(row)
    gm.mark_referenced(session, goods.id)
    return row


@router.post("/api/receipts", response_model=ReceiptOut, status_code=201, summary="记一笔入库验收")
def create_receipt(payload: PurchaseIn, session: Session = Depends(get_db)) -> Receipt:
    require_shop(session, payload.shop_id)
    row = _write_receipt(session, payload)
    session.commit()
    session.refresh(row)
    return row


@router.post("/api/receipts/bulk", response_model=BulkResult, summary="批量入库（数据灌入用）")
def create_receipts_bulk(
    shop_id: int = Query(...),
    items: list[PurchaseIn] = Body(...),
    session: Session = Depends(get_db),
) -> BulkResult:
    require_shop(session, shop_id)
    result = BulkResult()
    for item in items:
        try:
            if item.shop_id != shop_id:
                raise ValueError("shop_id 与路径参数不一致")
            _write_receipt(session, item)
            result.inserted += 1
        except Exception as exc:                     # noqa: BLE001
            result.errors.append(f"{item.purchase_date} goods={item.goods_id}: {exc}")
    session.commit()
    return result


@router.get("/api/receipts", response_model=list[ReceiptOut], summary="按区间列出入库单")
def list_receipts(
    shop_id: int = Query(...),
    start: date = Query(...),
    end: date = Query(...),
    session: Session = Depends(get_db),
) -> list[Receipt]:
    require_shop(session, shop_id)
    if start > end:
        start, end = end, start
    return list(
        session.scalars(
            select(Receipt)
            .where(
                Receipt.shop_id == shop_id,
                Receipt.receipt_date >= start,
                Receipt.receipt_date <= end,
            )
            .order_by(Receipt.receipt_date, Receipt.id)
        ).all()
    )


# =========================================================================== #
# 盘点单 / 报废单
# =========================================================================== #
def _stocktake_out(row: Stocktake) -> StocktakeOut:
    """补上原因代码的中文名（前端不必自己维护 T01–T08 字典）。"""
    out = StocktakeOut.model_validate(row)
    out.reason_name = approvals.LOSS_REASONS.get(row.reason_code or "", None)
    return out


@router.put("/api/stocktakes", response_model=StocktakeOut, summary="录入/覆盖一张盘点单")
def upsert_stocktake(payload: StocktakeIn, session: Session = Depends(get_db)) -> StocktakeOut:
    require_shop(session, payload.shop_id)
    goods = session.get(Goods, payload.goods_id)
    if goods is None:
        raise HTTPException(status_code=404, detail=f"商品 {payload.goods_id} 不存在")
    price = (
        float(payload.unit_price) if payload.unit_price is not None else float(goods.unit_price)
    )

    row = session.scalars(
        select(Stocktake).where(
            Stocktake.shop_id == payload.shop_id,
            Stocktake.goods_id == payload.goods_id,
            Stocktake.take_date == payload.take_date,
        )
    ).first()
    if row is None:
        row = Stocktake(
            shop_id=payload.shop_id, goods_id=payload.goods_id, take_date=payload.take_date
        )
        session.add(row)

    from ..stock_service import compute_book_qty, effective_book_qty

    # ⚠️ 先落库字段，再用**唯一实现** effective_book_qty 取账面。
    # M5 自查（审查项 C2 / E2）抓到过这里的 bug：早先自己写了一份
    # "老板自报优先"的口径，和 stock_service 的"系统推算优先"相反 ——
    # 结果同一张盘点单，签字时按 100 算差异、看报告时按 150 算，数字对不上。
    # 差异判定和差异展示必须是**同一个数**，否则审批就是在签一个看不见的数。
    row.book_qty = payload.book_qty
    row.actual_qty = payload.actual_qty

    computed, why = compute_book_qty(session, payload.shop_id, payload.goods_id, payload.take_date)
    effective_book = effective_book_qty(session, row, computed)
    variance_qty = float(payload.actual_qty) - float(effective_book or 0.0)
    variance_amount = round(variance_qty * price, 6)
    book_amount = round(float(effective_book or 0.0) * price, 6)

    # M5-2：盘盈盘亏超阈值（±100 元或 ±5%）必须有原因代码 + 审批人
    try:
        reason_code = approvals.normalize_reason(payload.reason_code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 期初建账（没有上期实盘）不算盈亏：账面从 0 起算，差异 = 全部库存。
    # 对建账要"原因代码和审批人"是荒谬的 —— 老板第一天把货录进来，不是丢了货。
    if why.get("has_base"):
        try:
            approvals.require_approval(
                amount=variance_amount,
                base=book_amount,
                approver=payload.approver,
                what=f"盘亏 {abs(variance_amount):,.0f} 元",
            )
        except approvals.ApprovalRequiredError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 有差异就必须给原因 —— 否则"差异"永远只是个数字，回答不了"漏在哪一环"
        if abs(variance_amount) > 0.005 and reason_code is None:
            raise HTTPException(
                status_code=400,
                detail="盘盈盘亏必须选一个原因代码（T01–T08），不然这笔差异以后没法归类统计。",
            )

    row.computed_book_qty = round(computed, 6)
    row.unit_price = price
    row.reason_code = reason_code
    row.approver = payload.approver
    row.note = payload.note
    session.commit()
    session.refresh(row)
    return _stocktake_out(row)


@router.get("/api/stocktakes", response_model=list[StocktakeOut], summary="按区间列出盘点单")
def list_stocktakes(
    shop_id: int = Query(...),
    start: date = Query(...),
    end: date = Query(...),
    session: Session = Depends(get_db),
) -> list[StocktakeOut]:
    require_shop(session, shop_id)
    if start > end:
        start, end = end, start
    rows = session.scalars(
        select(Stocktake)
        .where(
            Stocktake.shop_id == shop_id,
            Stocktake.take_date >= start,
            Stocktake.take_date <= end,
        )
        .order_by(Stocktake.take_date, Stocktake.id)
    ).all()
    return [_stocktake_out(r) for r in rows]


@router.delete("/api/stocktakes/{stocktake_id}", status_code=204, summary="删除盘点单")
def delete_stocktake(stocktake_id: int, session: Session = Depends(get_db)) -> None:
    session.execute(delete(Stocktake).where(Stocktake.id == stocktake_id))
    session.commit()


def _scrap_out(row: Scrap) -> ScrapOut:
    """补上原因代码的中文名 —— 前端不必自己维护一份 T01–T08 字典。"""
    out = ScrapOut.model_validate(row)
    out.reason_name = approvals.LOSS_REASONS.get(row.reason_code or "", None)
    return out


@router.post("/api/scraps", response_model=ScrapOut, status_code=201, summary="记一笔报废")
def create_scrap(payload: ScrapIn, session: Session = Depends(get_db)) -> ScrapOut:
    """M5-2：原因**必须代码化**（T01–T08），超阈值必须有审批人。

    自由文本里的"坏了""倒了""扔了"没法聚合 —— 报表明明有数据，
    却回答不了"我的损耗主要发生在哪一环"。所以这里不接受自由文本当原因。
    """
    require_shop(session, payload.shop_id)
    goods = session.get(Goods, payload.goods_id)
    if goods is None:
        raise HTTPException(status_code=404, detail=f"商品 {payload.goods_id} 不存在")
    price = float(payload.unit_price) if payload.unit_price is not None else float(goods.unit_price)
    amount = round(float(payload.qty) * price, 6)

    try:
        reason_code = approvals.normalize_reason(payload.reason_code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if reason_code is None:
        raise HTTPException(
            status_code=400,
            detail="报废必须选一个原因代码（T01–T08）。写「坏了」以后统计不出是哪一环漏的。",
        )
    # 阈值基数 = **当月这个货进了多少货**（损耗率的分母标准做法：报损 ÷ 进货）。
    # 不用"累计报废"当基数 —— 那永远追不上自己，抓不到"进 50 元报掉 45 元"这种。
    # 没有采购数据时基数为空 → 只按 100 元判断（宁可松，也不拿一个假分母去算比例）。
    month_start = payload.scrap_date.replace(day=1)
    month_base = float(
        session.scalar(
            select(func.coalesce(func.sum(Purchase.amount), 0.0)).where(
                Purchase.shop_id == payload.shop_id,
                Purchase.goods_id == payload.goods_id,
                Purchase.purchase_date >= month_start,
                Purchase.purchase_date <= payload.scrap_date,
            )
        )
        or 0.0
    )
    try:
        approvals.require_approval(
            amount=amount,
            base=month_base or None,
            approver=payload.approver,
            what=f"这笔报废 {amount:,.0f} 元",
        )
    except approvals.ApprovalRequiredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    row = Scrap(
        shop_id=payload.shop_id,
        goods_id=payload.goods_id,
        scrap_date=payload.scrap_date,
        qty=payload.qty,
        units_lost=payload.units_lost,
        unit_price=price,
        amount=amount,
        reason_code=reason_code,
        approver=payload.approver,
        photo_url=payload.photo_url,
        reason=payload.reason,
        note=payload.note,
    )
    session.add(row)
    gm.mark_referenced(session, goods.id)
    session.commit()
    session.refresh(row)
    return _scrap_out(row)


@router.get("/api/scraps", response_model=list[ScrapOut], summary="按区间列出报废单")
def list_scraps(
    shop_id: int = Query(...),
    start: date = Query(...),
    end: date = Query(...),
    session: Session = Depends(get_db),
) -> list[ScrapOut]:
    require_shop(session, shop_id)
    if start > end:
        start, end = end, start
    rows = session.scalars(
        select(Scrap)
        .where(
            Scrap.shop_id == shop_id,
            Scrap.scrap_date >= start,
            Scrap.scrap_date <= end,
        )
        .order_by(Scrap.scrap_date, Scrap.id)
    ).all()
    return [_scrap_out(r) for r in rows]


@router.get("/api/scraps/reasons", response_model=list[LossReasonOut], summary="损耗原因代码字典")
def loss_reasons() -> list[LossReasonOut]:
    """前端下拉直接用它渲染 —— 以后加代码不用改前端。"""
    return [LossReasonOut(code=k, name=v) for k, v in approvals.LOSS_REASONS.items()]


@router.get("/api/scraps/breakdown", response_model=list[LossReasonBreakdown], summary="损耗按原因聚合")
def loss_breakdown(
    shop_id: int = Query(...),
    start: date = Query(...),
    end: date = Query(...),
    session: Session = Depends(get_db),
) -> list[LossReasonBreakdown]:
    """这就是"原因代码化"换来的东西：回答"损耗主要发生在哪一环"。"""
    require_shop(session, shop_id)
    if start > end:
        start, end = end, start
    rows = session.scalars(
        select(Scrap).where(
            Scrap.shop_id == shop_id,
            Scrap.scrap_date >= start,
            Scrap.scrap_date <= end,
        )
    ).all()
    return [LossReasonBreakdown.model_validate(i) for i in approvals.reason_breakdown(rows)]


# =========================================================================== #
# 账实差异报表
# =========================================================================== #
@router.get("/api/stock/variance", summary="账实差异报表（逐货品 + 一句话）")
def stock_variance_report(
    shop_id: int = Query(...),
    start: date = Query(...),
    end: date = Query(...),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """差异率由**注册指标**算（`stock_variance_rate`，公式在 metric_def 里），
    明细来自 `stock_service.variance_detail()`（账面库存的唯一实现）。
    """
    from ..metrics_engine import MetricsEngine

    require_shop(session, shop_id)
    if start > end:
        start, end = end, start
    values = MetricsEngine(session).compute(shop_id, start, end)
    session.commit()
    payload = _stock_variance(session, shop_id, start, end, values.get("stock_variance_rate"))
    payload["book_qty_detail"] = variance_detail(session, shop_id, start, end)
    return payload
