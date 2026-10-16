"""供应商档案与应付账款（M5-1）。

授权依据：`docs/05-M5长程施工任务书.md` §4（字段口径 v0.2 §3.1，评级 v0.2 §3.1.1）。

## 评级为什么要照抄权重而不是自己调

   准时交付率 30% + 质量合格率 30% + 价格稳定性 20% + 损耗贡献 20%
   A ≥ 90 / B ≥ 80 / C ≥ 70 / D < 70

这_weights 是蓝图给的，不是我拍的。**改权重等于改口径**，老板拿到的评级就没法跟同行比。
所以这里只做一件事：把四项原始观测算准（尤其是"样本不足时不评价"，见下）。

## 两个"不误伤"的默认

* 没有配送记录 → 准时率/合格率按 **1.0**（不评价），不是 0 分。
* 采购样本 < 2 条 → 价格稳定性按 **1.0**。

理由：新供应商刚建档就被打成 D，老板会以为这家很差，其实是**没数据**。
"不知道"和"很差"必须分开表达。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import supplier_service as ss
from ..db import get_db
from ..models import Payable, Supplier, _now
from ..schemas import (
    ConcentrationOut,
    PayableIn,
    PayableOut,
    PayablesAgingOut,
    SupplierIn,
    SupplierOut,
    SupplierRatingOut,
)
from .daily import require_shop

router = APIRouter(tags=["suppliers"])


def _payable_out(session: Session, row: Payable, as_of: date | None = None) -> PayableOut:
    supplier = session.get(Supplier, row.supplier_id)
    out = PayableOut.model_validate(row)
    out.supplier_name = supplier.name if supplier else None
    out.payable_amount = row.payable_amount
    out.outstanding = row.outstanding
    ref = as_of or date.today()
    out.overdue_days = (ref - row.due_date).days
    return out


# =========================================================================== #
# 供应商档案
# =========================================================================== #
@router.post("/api/suppliers", response_model=SupplierOut, status_code=201, summary="建供应商档案")
def create_supplier(payload: SupplierIn, session: Session = Depends(get_db)) -> SupplierOut:
    require_shop(session, payload.shop_id)
    name = payload.name.strip()
    dup = session.scalars(
        select(Supplier).where(Supplier.shop_id == payload.shop_id, Supplier.name == name)
    ).first()
    if dup is not None:
        raise HTTPException(status_code=409, detail=f"已经有同名供应商「{name}」了，别建两份")
    row = Supplier(**payload.model_dump())
    session.add(row)
    session.commit()
    session.refresh(row)
    return SupplierOut.model_validate(row)


@router.get("/api/suppliers", response_model=list[SupplierOut], summary="供应商列表")
def list_suppliers(
    shop_id: int = Query(...),
    enabled_only: bool = Query(default=True),
    session: Session = Depends(get_db),
) -> list[SupplierOut]:
    require_shop(session, shop_id)
    stmt = select(Supplier).where(Supplier.shop_id == shop_id)
    if enabled_only:
        stmt = stmt.where(Supplier.enabled == 1)
    return [SupplierOut.model_validate(r) for r in session.scalars(stmt.order_by(Supplier.id)).all()]


@router.delete("/api/suppliers/{supplier_id}", status_code=204, summary="停用供应商")
def disable_supplier(supplier_id: int, session: Session = Depends(get_db)) -> None:
    row = session.get(Supplier, supplier_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"供应商 {supplier_id} 不存在")
    row.enabled = 0
    session.commit()


@router.get(
    "/api/suppliers/ratings",
    response_model=list[SupplierRatingOut],
    summary="供应商评级（准时 30% + 质量 30% + 价格稳定 20% + 损耗 20%）",
)
def supplier_ratings(
    shop_id: int = Query(...),
    start: date = Query(...),
    end: date = Query(...),
    session: Session = Depends(get_db),
) -> list[SupplierRatingOut]:
    require_shop(session, shop_id)
    suppliers = session.scalars(
        select(Supplier).where(Supplier.shop_id == shop_id, Supplier.enabled == 1)
    ).all()
    out: list[SupplierRatingOut] = []
    for s in suppliers:
        data = ss.supplier_metrics(session, s, start, end)
        data["period"] = {"start": start.isoformat(), "end": end.isoformat()}
        out.append(SupplierRatingOut.model_validate(data))
    # 评级低的排前面 —— 老板先看谁在拖后腿
    out.sort(key=lambda x: x.score)
    return out


@router.get(
    "/api/suppliers/concentration",
    response_model=ConcentrationOut,
    summary="供应商集中度（TOP1 占比）",
)
def concentration(
    shop_id: int = Query(...),
    start: date = Query(...),
    end: date = Query(...),
    session: Session = Depends(get_db),
) -> ConcentrationOut:
    require_shop(session, shop_id)
    data = ss.supplier_concentration(session, shop_id, start, end)
    data["period"] = {"start": start.isoformat(), "end": end.isoformat()}
    share = data.get("top1_share")
    if share is None:
        data["note"] = "这段区间还没有采购数据。"
    elif share >= 0.6:
        data["note"] = (
            f"最大一家占 {share:.0%} —— 这家一断供你就断货，建议至少留一家备选。"
        )
    elif share <= 0.2:
        data["note"] = f"最大一家只占 {share:.0%}，供应商很散 —— 量分散了，议价时不好压价。"
    else:
        data["note"] = f"最大一家占 {share:.0%}，集中度还行。"
    return ConcentrationOut.model_validate(data)


# =========================================================================== #
# 应付账款
# =========================================================================== #
@router.post("/api/payables", response_model=PayableOut, status_code=201, summary="记一笔应付")
def create_payable(payload: PayableIn, session: Session = Depends(get_db)) -> PayableOut:
    require_shop(session, payload.shop_id)
    supplier = session.get(Supplier, payload.supplier_id)
    if supplier is None:
        raise HTTPException(status_code=404, detail=f"供应商 {payload.supplier_id} 不存在")
    due = payload.due_date or payload.biz_date + timedelta(days=int(supplier.credit_days or 0))
    row = Payable(
        shop_id=payload.shop_id,
        supplier_id=payload.supplier_id,
        purchase_id=payload.purchase_id,
        biz_date=payload.biz_date,
        due_date=due,
        amount=float(payload.amount),
        discount_qty=float(payload.discount_qty or 0.0),
        discount_amount=float(payload.discount_amount or 0.0),
        paid_amount=float(payload.paid_amount or 0.0),
        note=payload.note,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _payable_out(session, row)


@router.get("/api/payables", response_model=list[PayableOut], summary="按区间列出应付")
def list_payables(
    shop_id: int = Query(...),
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    unsettled_only: bool = Query(default=False, description="只看还没结清的"),
    session: Session = Depends(get_db),
) -> list[PayableOut]:
    require_shop(session, shop_id)
    rows = session.scalars(
        select(Payable).where(Payable.shop_id == shop_id).order_by(Payable.due_date)
    ).all()
    out: list[PayableOut] = []
    for row in rows:
        item = _payable_out(session, row)
        if start and row.biz_date < start:
            continue
        if end and row.biz_date > end:
            continue
        if unsettled_only and item.outstanding <= 0.005:
            continue
        out.append(item)
    return out


@router.post("/api/payables/{payable_id}/pay", response_model=PayableOut, summary="记一笔付款（核销）")
def pay_payable(
    payable_id: int,
    amount: float = Query(..., gt=0, description="本次付了多少"),
    session: Session = Depends(get_db),
) -> PayableOut:
    """付多了按结清处理（小店常见"抹零多付两块钱"），不会挂成负数。"""
    row = session.get(Payable, payable_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"应付 {payable_id} 不存在")
    row.paid_amount = round(row.payable_amount, 6) if amount >= row.outstanding else round(
        float(row.paid_amount or 0.0) + float(amount), 6
    )
    if row.outstanding <= 0.005:
        row.settled_at = _now()
    session.commit()
    session.refresh(row)
    return _payable_out(session, row)


@router.get("/api/payables/aging", response_model=PayablesAgingOut, summary="应付账龄 + 逾期清单")
def payables_aging(
    shop_id: int = Query(...),
    as_of: date | None = Query(default=None),
    session: Session = Depends(get_db),
) -> PayablesAgingOut:
    """任务书 §4 验收要的那句话就在这里的 `sentence` 字段：
    「欠 3 家共 X 元，A 家还有 5 天到期，C 家已逾期 12 天」。
    """
    require_shop(session, shop_id)
    data: dict[str, Any] = ss.payables_aging(session, shop_id, as_of)
    return PayablesAgingOut.model_validate(data)
