"""原子数字录入（施工单 3.2 / server/routers/daily.py）。

边界情况（施工单 3.6）的落点：
    同一天重复保存 → 覆盖不新增（靠 uq_fact_daily_key + upsert）
    工时为空       → 不写入，不报错
    跨夜班         → shift_hours 里 +24h
    营业额为 0     → 照存 0，由引擎在除法处返回 None（不除零）

本文件还导出三个写入助手（upsert_fact / replace_shifts / apply_values），
供 routers/shops.py 写月口径原子值复用 —— 施工单没给 services 层，
不新开文件，就近复用。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Iterable, Sequence

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..metrics_engine import TYPED_FIELD_COLUMNS, shift_hours
from ..models import FactDaily, FieldDef, Shop, Shift
from ..schemas import DailyOut, DailyUpsertIn, FactOut, ShiftIn, ShiftOut

router = APIRouter(prefix="/api/daily", tags=["daily"])

PERIOD_DAY = "day"
PERIOD_MONTH = "month"


# --------------------------------------------------------------------------- #
# 共用助手
# --------------------------------------------------------------------------- #
def require_shop(session: Session, shop_id: int) -> Shop:
    shop = session.get(Shop, shop_id)
    if shop is None:
        raise HTTPException(status_code=404, detail=f"门店 {shop_id} 不存在")
    return shop


def enabled_fields(session: Session) -> dict[str, FieldDef]:
    rows = session.scalars(select(FieldDef).where(FieldDef.enabled == 1)).all()
    return {fd.code: fd for fd in rows}


def apply_values(session: Session, row: FactDaily, values: dict[str, float | None], scope: str) -> None:
    """把 values 写进 fact_daily 行。

    硬约束：字段 code 必须在 field_def 里、且 scope 与行口径一致。
    这条约束就是"加字段 = 插一行 field_def"能成立的原因 ——
    写进来的东西一定是注册过的，不会出现野字段。
    """
    fmap = enabled_fields(session)
    extra = _load_extra(row)

    for code, value in values.items():
        fd = fmap.get(code)
        if fd is None:
            raise HTTPException(
                status_code=400,
                detail=f"字段 {code!r} 不在 field_def 中（或已停用）。要加字段请先插 field_def 一行。",
            )
        field_scope = fd.scope or "day"
        if field_scope != scope:
            raise HTTPException(
                status_code=400,
                detail=f"字段 {code!r} 是 {field_scope} 口径，不能写进 {scope} 口径的行。",
            )
        if value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"字段 {code!r} 必须是数字")

        if code in TYPED_FIELD_COLUMNS:
            setattr(row, code, value)
        elif value is None:
            extra.pop(code, None)
        else:
            extra[code] = value

    row.extra_json = json.dumps(extra, ensure_ascii=False) if extra else None


def _load_extra(row: FactDaily) -> dict:
    if not row.extra_json:
        return {}
    try:
        data = json.loads(row.extra_json)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def upsert_fact(
    session: Session,
    shop_id: int,
    stat_date: date,
    period_type: str,
    values: dict[str, float | None] | None = None,
    note: str | None = None,
) -> FactDaily:
    row = session.scalars(
        select(FactDaily).where(
            FactDaily.shop_id == shop_id,
            FactDaily.stat_date == stat_date,
            FactDaily.period_type == period_type,
        )
    ).first()

    if row is None:
        row = FactDaily(shop_id=shop_id, stat_date=stat_date, period_type=period_type)
        session.add(row)
        session.flush()

    if values:
        apply_values(session, row, values, scope=period_type)
    if note is not None:
        row.note = note
    session.flush()
    return row


def replace_shifts(
    session: Session, shop: Shop, work_date: date, shifts: Sequence[ShiftIn]
) -> list[Shift]:
    """整天替换。hours<=0 的行静默跳过（施工单 3.6：工时为空不写入、不报错）。

    hourly_rate 存**生效值**（没填就落门店默认时薪），这样历史工时的成本口径
    不会被以后调默认时薪改掉。
    """
    session.execute(
        delete(Shift).where(Shift.shop_id == shop.id, Shift.work_date == work_date)
    )
    rows: list[Shift] = []
    for item in shifts:
        hours = shift_hours(item.start_time, item.end_time)
        if hours <= 0:
            continue
        rate = item.hourly_rate if item.hourly_rate is not None else shop.default_hourly_rate
        row = Shift(
            shop_id=shop.id,
            work_date=work_date,
            worker_name=item.worker_name,
            start_time=item.start_time,
            end_time=item.end_time,
            hours=hours,
            hourly_rate=rate,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def fact_values(row: FactDaily | None, fmap: dict[str, FieldDef], period_type: str) -> dict[str, float | None]:
    values: dict[str, float | None] = {}
    if row is None:
        return values
    extra = _load_extra(row)
    for code, fd in fmap.items():
        if (fd.scope or "day") != period_type:
            continue
        values[code] = getattr(row, code) if code in TYPED_FIELD_COLUMNS else extra.get(code)
    return values


def _shift_outs(rows: Iterable[Shift]) -> list[ShiftOut]:
    return [
        ShiftOut(
            id=s.id,
            worker_name=s.worker_name,
            start_time=s.start_time,
            end_time=s.end_time,
            hours=s.hours,
            hourly_rate=s.hourly_rate,
        )
        for s in rows
    ]


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@router.put("", response_model=DailyOut, summary="录入/覆盖某一天的原子数字与工时")
def upsert_daily(payload: DailyUpsertIn, session: Session = Depends(get_db)) -> DailyOut:
    if not payload.values and not payload.shifts:
        raise HTTPException(status_code=400, detail="至少要填一个数或一条排班，空提交不入库")

    shop = require_shop(session, payload.shop_id)
    row = upsert_fact(session, shop.id, payload.stat_date, PERIOD_DAY, payload.values, payload.note)
    shift_rows = replace_shifts(session, shop, payload.stat_date, payload.shifts)
    session.commit()

    fmap = enabled_fields(session)
    return DailyOut(
        stat_date=row.stat_date.isoformat(),
        period_type=row.period_type,
        values=fact_values(row, fmap, PERIOD_DAY),
        note=row.note,
        shifts=_shift_outs(shift_rows),
    )


@router.get("", response_model=list[DailyOut], summary="按区间列出每天的录入")
def list_daily(
    shop_id: int = Query(..., description="门店 id"),
    start: date = Query(..., description="起始日 YYYY-MM-DD"),
    end: date = Query(..., description="结束日 YYYY-MM-DD"),
    session: Session = Depends(get_db),
) -> list[DailyOut]:
    require_shop(session, shop_id)
    if start > end:
        start, end = end, start

    rows = session.scalars(
        select(FactDaily)
        .where(
            FactDaily.shop_id == shop_id,
            FactDaily.period_type == PERIOD_DAY,
            FactDaily.stat_date >= start,
            FactDaily.stat_date <= end,
        )
        .order_by(FactDaily.stat_date)
    ).all()

    shift_rows = session.scalars(
        select(Shift)
        .where(Shift.shop_id == shop_id, Shift.work_date >= start, Shift.work_date <= end)
        .order_by(Shift.work_date, Shift.id)
    ).all()
    by_date: dict[str, list[Shift]] = {}
    for s in shift_rows:
        by_date.setdefault(s.work_date.isoformat(), []).append(s)

    fmap = enabled_fields(session)
    return [
        DailyOut(
            stat_date=row.stat_date.isoformat(),
            period_type=row.period_type,
            values=fact_values(row, fmap, PERIOD_DAY),
            note=row.note,
            shifts=_shift_outs(by_date.get(row.stat_date.isoformat(), [])),
        )
        for row in rows
    ]


@router.delete("/{shop_id}/{stat_date}", status_code=204, summary="删除某一天（含当天排班）")
def delete_daily(
    shop_id: int = Path(...),
    stat_date: date = Path(...),
    session: Session = Depends(get_db),
) -> None:
    require_shop(session, shop_id)
    session.execute(
        delete(Shift).where(Shift.shop_id == shop_id, Shift.work_date == stat_date)
    )
    session.execute(
        delete(FactDaily).where(
            FactDaily.shop_id == shop_id,
            FactDaily.stat_date == stat_date,
            FactDaily.period_type == PERIOD_DAY,
        )
    )
    session.commit()
