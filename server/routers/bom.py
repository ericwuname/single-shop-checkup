"""成本卡（BOM）与量价差异（M5-0）。

授权依据：`docs/05-M5长程施工任务书.md` §3。

## 三条设计决定

1. **标准毛用量由后端算**（`std_qty_gross = std_qty_net / yield_rate`），不接受前端传。
   让调用方传 = 迟早出现"毛用量和净用量对不上"的脏数据。
2. **改配方 = 新版本**，旧版本置 `enabled=0` 而不是覆盖 —— 与"事实不可变"同一个道理，
   否则上个月的差异算出来跟当时不一样。
3. **差异拆量差/价差**：量差找厨房、价差找采购。不拆就不知道该找谁。

## 为什么没有"领料单"表

任务书 §3 只授权建 `bom` 与 `bom_variance`，没有领料单。
所以"领用"按**标准用量**登记（`std_usage`），
实盘与「入库 − 理论领用」的差，正好就是跑冒滴漏 —— 这就是"差异变纯"。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import bom_service as bs
from ..db import get_db
from ..models import Bom, BomVariance, Goods
from ..schemas import (
    BomIn,
    BomOut,
    BomVarianceIn,
    BomVarianceOut,
    BomVarianceSummaryOut,
    CountingDueOut,
)
from .daily import require_shop

router = APIRouter(tags=["bom"])

#: 出成率没填时的告警话术（v1.0 §3.2：不填出成率，标准成本必然算低）
_YIELD_WARNING = (
    "没填出成率，标准成本按净料算 —— 采购是按毛料付钱的，"
    "毛净差会一直体现在差异里。补齐出成率后这个数字才准。"
)


def _goods_name(session: Session, goods_id: int) -> str | None:
    goods = session.get(Goods, goods_id)
    return goods.name if goods else None


def _bom_out(session: Session, row: Bom) -> BomOut:
    payload = BomOut.model_validate(row)
    payload.goods_name = _goods_name(session, row.goods_id)
    payload.std_cost = round(float(row.std_qty_gross) * float(row.std_price or 0.0), 4)
    # 出成率留空（=1.0）时给告警：这是"默认不评价"而不是"默认没损耗"
    payload.warning = None if float(row.yield_rate or 1.0) < 1.0 else _YIELD_WARNING
    return payload


@router.post("/api/bom", response_model=BomOut, status_code=201, summary="建/改成本卡（改配方 = 新版本）")
def upsert_bom(payload: BomIn, session: Session = Depends(get_db)) -> BomOut:
    require_shop(session, payload.shop_id)
    try:
        row = bs.build_bom_row(
            session,
            shop_id=payload.shop_id,
            dish_name=payload.dish_name.strip(),
            goods_id=payload.goods_id,
            std_qty_net=payload.std_qty_net,
            yield_rate=float(payload.yield_rate or 1.0),
            unit=payload.unit,
            std_price=payload.std_price,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    session.refresh(row)
    return _bom_out(session, row)


@router.get("/api/bom", response_model=list[BomOut], summary="成本卡列表（可按菜名查）")
def list_bom(
    shop_id: int = Query(...),
    dish_name: str | None = Query(default=None),
    goods_id: int | None = Query(default=None),
    enabled_only: bool = Query(default=True, description="只看生效版本"),
    session: Session = Depends(get_db),
) -> list[BomOut]:
    require_shop(session, shop_id)
    stmt = select(Bom).where(Bom.shop_id == shop_id)
    if enabled_only:
        stmt = stmt.where(Bom.enabled == 1)
    if dish_name:
        stmt = stmt.where(Bom.dish_name == dish_name.strip())
    if goods_id is not None:
        stmt = stmt.where(Bom.goods_id == goods_id)
    stmt = stmt.order_by(Bom.dish_name, Bom.goods_id, Bom.version.desc())
    return [_bom_out(session, row) for row in session.scalars(stmt).all()]


@router.delete("/api/bom/{bom_id}", status_code=204, summary="停用成本卡（配方下架）")
def disable_bom(bom_id: int, session: Session = Depends(get_db)) -> None:
    row = session.get(Bom, bom_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"成本卡 {bom_id} 不存在")
    row.enabled = 0
    session.commit()


@router.post(
    "/api/bom/variance",
    response_model=BomVarianceOut,
    status_code=201,
    summary="记一条量价差异（理论用量自动算）",
)
def create_variance(payload: BomVarianceIn, session: Session = Depends(get_db)) -> BomVarianceOut:
    """实际用量**可以不填** —— 系统从盘点反推（能算的就不让老板填）。"""
    require_shop(session, payload.shop_id)
    try:
        row = bs.record_variance(
            session,
            shop_id=payload.shop_id,
            biz_date=payload.biz_date,
            dish_name=payload.dish_name.strip(),
            goods_id=payload.goods_id,
            sold_qty=payload.sold_qty,
            actual_usage=payload.actual_usage,
            actual_price=payload.actual_price,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row.note = payload.note
    session.commit()
    session.refresh(row)
    out = BomVarianceOut.model_validate(row)
    out.goods_name = _goods_name(session, row.goods_id)
    return out


@router.get("/api/bom/variance", response_model=list[BomVarianceOut], summary="按区间列出差异记录")
def list_variance(
    shop_id: int = Query(...),
    start: date = Query(...),
    end: date = Query(...),
    session: Session = Depends(get_db),
) -> list[BomVarianceOut]:
    require_shop(session, shop_id)
    rows = session.scalars(
        select(BomVariance)
        .where(
            BomVariance.shop_id == shop_id,
            BomVariance.biz_date >= start,
            BomVariance.biz_date <= end,
        )
        .order_by(BomVariance.biz_date, BomVariance.id)
    ).all()
    out: list[BomVarianceOut] = []
    cache: dict[int, str | None] = {}
    for row in rows:
        item = BomVarianceOut.model_validate(row)
        if row.goods_id not in cache:
            cache[row.goods_id] = _goods_name(session, row.goods_id)
        item.goods_name = cache[row.goods_id]
        out.append(item)
    return out


@router.get(
    "/api/bom/variance/summary",
    response_model=BomVarianceSummaryOut,
    summary="量差 / 价差汇总（谁的问题分开说）",
)
def variance_summary(
    shop_id: int = Query(...),
    start: date = Query(...),
    end: date = Query(...),
    session: Session = Depends(get_db),
) -> BomVarianceSummaryOut:
    require_shop(session, shop_id)
    data = bs.variance_summary(session, shop_id, start, end)
    data["period"] = {"start": start.isoformat(), "end": end.isoformat()}
    return BomVarianceSummaryOut.model_validate(data)


@router.get("/api/stock/counting-due", response_model=CountingDueOut, summary="本期待盘清单（ABC 抽盘）")
def counting_due(
    shop_id: int = Query(...),
    as_of: date | None = Query(default=None),
    session: Session = Depends(get_db),
) -> CountingDueOut:
    """A 类每周、B 类每月、C 类每季 —— 只盘 A 类能抓 80% 的差异。"""
    require_shop(session, shop_id)
    data: dict[str, Any] = bs.counting_due(session, shop_id, as_of)
    data["items"] = [
        {
            "goods_id": i["goods_id"],
            "code": i["code"],
            "name": i["name"],
            "spec": i["spec"],
            "abc_class": i["abc_class"],
            "cycle_days": i["cycle_days"],
            "last_take_date": i["last_take_date"],
            "days_since_last": i["days_since_last"],
            "due": i["due"],
        }
        for i in data["items"]
    ]
    return CountingDueOut.model_validate(data)
