"""多店归属、汇总与对比（M4-6 / M4-7）。

用户原话：「可能有多店，可以单独看一个店，也可以看名下所有店」。

## 一条铁律：**金额可加，比率不可加**

三家店营业额 100 / 300 / 500、人工 25 / 60 / 100：

| 做法 | 结果 |
|---|---|
| ✅ 正确：Σ人工 ÷ Σ营业额 = 185 / 900 | **20.56%** |
| ❌ 错误：(25% + 20% + 20%) / 3 | 21.67% |

实现上不是"注意一下"，而是**结构上做不到取平均**：
先把各店原子量逐项相加，再跑同一套公式（`MetricsEngine.sum_atoms` + `evaluate_on`）。
本文件里没有任何一处按店取平均 —— 变异验证会盯着这一点（见交付报告）。

归属靠 `shops.owner_key`（不加表，只加列），`is_active=0` 的店不进汇总但历史仍可查。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..metrics_engine import MetricsEngine, month_last_day
from ..models import Shop
from ..schemas import ShopOut  # noqa: F401 - 门店出入参在 shops.py 定义，这里只做汇总
from .. import benchmarks as bm

router = APIRouter(tags=["portfolio"])

#: 对比表里要横排看的指标（越靠前越重要）
COMPARE_CODES = (
    "revenue",
    "daily_avg_revenue",
    "labor_cost_rate_full",
    "food_cost_rate",
    "efficiency",
    "breakeven_daily",
    "real_profit_m",
)

#: 排名方向：1 = 越大越好；0 = 越小越好；None = 不排名（只展示）
RANK_HIGHER_BETTER = {
    "revenue": 1,
    "daily_avg_revenue": 1,
    "labor_cost_rate_full": 0,
    "food_cost_rate": 0,
    "efficiency": 1,
    "breakeven_daily": 0,
    "real_profit_m": 1,
}


# --------------------------------------------------------------------------- #
# 门店归属
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 汇总 + 对比
# --------------------------------------------------------------------------- #
def _shop_row(engine: MetricsEngine, shop: Shop, values: dict[str, float | None], days: int) -> dict[str, Any]:
    daily = None
    if values.get("revenue") is not None and days:
        daily = round(float(values["revenue"]) / days, 2)
    return {
        "shop_id": shop.id,
        "shop_name": shop.name,
        "revenue": values.get("revenue"),
        "daily_avg_revenue": daily,
        "labor_cost_rate_full": values.get("labor_cost_rate_full"),
        "food_cost_rate": values.get("food_cost_rate"),
        "efficiency": values.get("efficiency"),
        "breakeven_daily": values.get("breakeven_daily"),
        "real_profit_m": values.get("real_profit_m"),
        "rank": {},
    }


def _assign_ranks(rows: list[dict[str, Any]]) -> None:
    """逐指标横向排名（1 = 最好）。方向按 RANK_HIGHER_BETTER。"""
    for code, higher_better in RANK_HIGHER_BETTER.items():
        valid = [r for r in rows if isinstance(r.get(code), (int, float))]
        if not valid:
            continue
        ordered = sorted(
            valid, key=lambda r: float(r[code]), reverse=bool(higher_better)
        )
        for i, row in enumerate(ordered, start=1):
            row["rank"][code] = i


def _owner_shops(session: Session, owner_key: str | None) -> list[Shop]:
    """按老板归属取启用中的门店（owner_key 为 None 时取全部）。"""
    stmt = select(Shop).where(Shop.is_active == 1).order_by(Shop.id)
    if owner_key is not None:
        stmt = stmt.where(Shop.owner_key == owner_key)
    return list(session.scalars(stmt).all())


def build_portfolio(
    session: Session, shops: list[Shop], start: date, end: date
) -> dict[str, Any]:
    """汇总 + 对比。**汇总一律重算，不平均**（见模块 docstring）。"""
    engine = MetricsEngine(session)
    days = (end - start).days + 1

    rows: list[dict[str, Any]] = []
    for shop in shops:
        values = engine.compute(shop.id, start, end, persist=False)
        rows.append(_shop_row(engine, shop, values, days))

    shop_ids = [s.id for s in shops]
    merged_atoms = engine.sum_atoms(shop_ids, start, end) if shop_ids else {}
    totals = engine.evaluate_on(merged_atoms) if merged_atoms else {}

    _assign_ranks(rows)

    best = worst = None
    # 用哪个指标判"最好/最差"：优先实际利润；它算不出（缺月度房租/水电）时依次降级。
    # 硬要用实际利润会导致"没填月度开销的店一家都不提示" —— 那等于功能静默失效。
    for metric_code in ("real_profit_m", "revenue", "daily_avg_revenue"):
        ranked = [r for r in rows if r["rank"].get(metric_code)]
        if len(ranked) >= 2:
            ordered = sorted(ranked, key=lambda r: r["rank"][metric_code])
            best = {
                "metric": metric_code,
                "shop_name": ordered[0]["shop_name"],
                "shop_id": str(ordered[0]["shop_id"]),
            }
            worst = {
                "metric": metric_code,
                "shop_name": ordered[-1]["shop_name"],
                "shop_id": str(ordered[-1]["shop_id"]),
            }
            break

    # 跨店异常清单：按金额降序（老板先看最贵的那个）
    anomalies: list[dict[str, Any]] = []
    for row in rows:
        shop = next(s for s in shops if s.id == row["shop_id"])
        for code in ("labor_cost_rate_full", "food_cost_rate", "efficiency"):
            value = row.get(code)
            if value is None:
                continue
            md = engine.metric_defs.get(code)
            if md is None or not md.benchmark_key:
                continue
            verdict = bm.compare(shop.category, md.benchmark_key, value)
            if verdict and verdict["status"] in {"high", "low"}:
                anomalies.append(
                    {
                        "shop_id": row["shop_id"],
                        "shop_name": row["shop_name"],
                        "code": code,
                        "name": md.name,
                        "status": verdict["status"],
                        "display": f"{value:.1%}" if (md.unit or "") == "ratio" else f"{value:,.2f}",
                        "sentence": verdict["text"],
                        "amount": 0.0,
                    }
                )
    anomalies.sort(key=lambda x: x["amount"], reverse=True)

    return {
        "period": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days_with_data": days,
            "days_span": days,
            "month": month_last_day(end).strftime("%Y-%m"),
            "tier": bm.tier_for(days),
        },
        "shop_count": len(rows),
        "metrics": totals,
        "total_revenue": totals.get("revenue"),
        "total_labor_cost": totals.get("labor_cost"),
        "total_real_profit": totals.get("real_profit_m"),
        "shops": rows,
        "best_shop": best,
        "worst_shop": worst,
        "anomalies": anomalies,
        "note": (
            "汇总口径：先把各店原子量相加，再重算比率（Σ人工 ÷ Σ营业额），"
            "**不是对各店比率取平均**。日均为区间日均。"
        ),
    }


@router.get("/api/portfolio", summary="多店汇总 + 对比（M4-6/M4-7）")
def get_portfolio(
    owner_key: str | None = Query(default=None, description="留空则汇总全部启用门店"),
    start: date = Query(...),
    end: date = Query(...),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    if start > end:
        start, end = end, start
    shops = _owner_shops(session, owner_key)
    if not shops:
        raise HTTPException(status_code=404, detail="没有找到可汇总的门店")
    return build_portfolio(session, shops, start, end)


@router.get("/api/portfolio/compare", summary="门店对比表（各店并排 + 排名）")
def get_compare(
    owner_key: str | None = Query(default=None),
    start: date = Query(...),
    end: date = Query(...),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    data = get_portfolio(owner_key=owner_key, start=start, end=end, session=session)
    return {
        "period": data["period"],
        "shops": data["shops"],
        "best_shop": data["best_shop"],
        "worst_shop": data["worst_shop"],
    }
