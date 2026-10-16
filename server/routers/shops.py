"""门店 + 月口径原子值（施工单 3.2 / server/routers/shops.py）。

月口径（房租 / 水电 / 毛利率）为什么不单开表：
    施工单风险清单 #3「M1 期间禁止加业务表」。存在 fact_daily 里，用
    period_type='month' + stat_date=该月 1 号 承载，靠 field_def.scope 区分口径。
"""

from __future__ import annotations

import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..metrics_engine import month_first_day
from ..models import Shop
from ..schemas import FactOut, MonthlyUpsertIn, ShopCreate, ShopOut, ShopUpdate
from .daily import enabled_fields, fact_values, require_shop, upsert_fact

router = APIRouter(prefix="/api/shops", tags=["shops"])

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
PERIOD_MONTH = "month"


def parse_month(month: str) -> date:
    if not MONTH_RE.match(month or ""):
        raise HTTPException(status_code=400, detail="month 必须是 YYYY-MM，例如 2026-10")
    year, mon = month.split("-")
    return date(int(year), int(mon), 1)


@router.post("", response_model=ShopOut, status_code=201, summary="建店（M1 通常只建一家）")
def create_shop(payload: ShopCreate, session: Session = Depends(get_db)) -> Shop:
    shop = Shop(**payload.model_dump())
    session.add(shop)
    session.commit()
    session.refresh(shop)
    return shop


@router.get("", response_model=list[ShopOut], summary="门店列表（M4-6：可按老板归属过滤）")
def list_shops(
    owner_key: str | None = Query(default=None, description="同一老板/法人的标识"),
    active_only: bool = Query(default=False, description="关店的不进汇总"),
    session: Session = Depends(get_db),
) -> list[Shop]:
    stmt = select(Shop).order_by(Shop.id)
    if owner_key is not None:
        stmt = stmt.where(Shop.owner_key == owner_key)
    if active_only:
        stmt = stmt.where(Shop.is_active == 1)
    return list(session.scalars(stmt).all())


@router.get("/{shop_id}", response_model=ShopOut, summary="门店详情")
def get_shop(shop_id: int, session: Session = Depends(get_db)) -> Shop:
    return require_shop(session, shop_id)


@router.patch("/{shop_id}", response_model=ShopOut, summary="改门店信息")
def update_shop(
    shop_id: int, payload: ShopUpdate, session: Session = Depends(get_db)
) -> Shop:
    shop = require_shop(session, shop_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(shop, key, value)
    session.commit()
    session.refresh(shop)
    return shop


@router.put("/{shop_id}/monthly", response_model=FactOut, summary="录入/覆盖某月的固定开销")
def upsert_monthly(
    shop_id: int, payload: MonthlyUpsertIn, session: Session = Depends(get_db)
) -> FactOut:
    shop = require_shop(session, shop_id)
    stat_date = parse_month(payload.month)
    row = upsert_fact(session, shop.id, stat_date, PERIOD_MONTH, payload.values)
    session.commit()

    fmap = enabled_fields(session)
    return FactOut(
        stat_date=row.stat_date.isoformat(),
        period_type=row.period_type,
        values=fact_values(row, fmap, PERIOD_MONTH),
        note=row.note,
    )


@router.get("/{shop_id}/monthly", response_model=FactOut, summary="读某月的固定开销")
def get_monthly(
    shop_id: int,
    month: str = Query(..., description="YYYY-MM"),
    session: Session = Depends(get_db),
) -> FactOut:
    require_shop(session, shop_id)
    stat_date = month_first_day(parse_month(month))

    from ..models import FactDaily  # 局部导入，避免顶部堆无关符号

    row = session.scalars(
        select(FactDaily).where(
            FactDaily.shop_id == shop_id,
            FactDaily.period_type == PERIOD_MONTH,
            FactDaily.stat_date == stat_date,
        )
    ).first()

    fmap = enabled_fields(session)
    return FactOut(
        stat_date=stat_date.isoformat(),
        period_type=PERIOD_MONTH,
        values=fact_values(row, fmap, PERIOD_MONTH),
        note=row.note if row else None,
    )
