"""供应商与应付账款（M5-1）。

字段口径按 v0.2 §3.1，评级规则按 v0.2 §3.1.1 **照抄**：

    准时交付率 30% + 质量合格率 30% + 价格稳定性 20% + 损耗贡献 20%
    A ≥ 90 / B ≥ 80 / C ≥ 70 / D < 70

## 这一模块回答的问题

"我还欠谁钱、多久要还、哪家供应商在涨价" —— 这是"钱漏在哪"的又一块。
账龄结构（0-30/31-60/61-90/90+）与逾期金额是老板最该看的两张脸。
"""

from __future__ import annotations

import statistics
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Goods, Payable, Purchase, Scrap, Supplier

#: 评级权重（照抄 v0.2 §3.1.1）
RATING_WEIGHTS = {
    "on_time": 0.30,        # 准时交付率
    "quality": 0.30,        # 质量合格率
    "price_stable": 0.20,   # 价格稳定性
    "loss": 0.20,           # 损耗贡献
}

#: 账龄分桶（天）。起算点是**到期日**（通行做法：真正重要的问题是逾期多久）。
#:
#: ⚠️ M5 自查（审查项 A4）改过一次：早先把"还没到期"的也塞进 0-30，
#: 导致"欠 1 万、全是下个月才到期"和"欠 1 万、全逾期 20 天"看起来是同一档。
#: 通行做法是把 **未逾期单独一列** —— 那是"你还能施加影响的部分"，
#: 右边每一档都是"已经做出的选择或已经出错的事"。两者混在一起，报表就失去指向性。
AGING_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("未到期", None, 0),
    ("1-30", 1, 30),
    ("31-60", 31, 60),
    ("61-90", 61, 90),
    ("90+", 91, None),
)


def grade_of(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    return "D"


def _price_stability(prices: list[float]) -> float:
    """价格稳定性 0–1：变异系数越小越稳。

    样本不足 2 条时给 1.0（**不评价**，而不是"给 0 分误伤"）。
    """
    clean = [p for p in prices if p and p > 0]
    if len(clean) < 2:
        return 1.0
    mean = statistics.fmean(clean)
    if mean <= 0:
        return 1.0
    cv = statistics.pstdev(clean) / mean
    return max(0.0, 1.0 - cv)


def supplier_metrics(
    session: Session, supplier: Supplier, start: date, end: date
) -> dict[str, Any]:
    """一个供应商在区间内的四项指标 + 综合评级。"""
    purchases = session.scalars(
        select(Purchase).where(
            Purchase.supplier_id == supplier.id,
            Purchase.purchase_date >= start,
            Purchase.purchase_date <= end,
        )
    ).all()
    buy_amount = round(sum(float(p.amount or 0.0) for p in purchases), 2)
    prices = [float(p.unit_price or 0.0) for p in purchases]

    on_time = (
        supplier.on_time_deliveries / supplier.total_deliveries
        if supplier.total_deliveries
        else 1.0
    )
    quality = (
        supplier.qualified_batches / supplier.total_batches if supplier.total_batches else 1.0
    )
    price_stable = _price_stability(prices)

    goods_ids = {p.goods_id for p in purchases}
    loss_amount = 0.0
    if goods_ids:
        loss_amount = float(
            session.scalar(
                select(func.coalesce(func.sum(Scrap.amount), 0.0)).where(
                    Scrap.shop_id == supplier.shop_id,
                    Scrap.goods_id.in_(goods_ids),
                    Scrap.scrap_date >= start,
                    Scrap.scrap_date <= end,
                )
            )
            or 0.0
        )
    # 损耗贡献 = 1 − 这家供的货报损占比（报损越多，这家越拖后腿）
    loss_score = 1.0 if buy_amount <= 0 else max(0.0, 1.0 - loss_amount / buy_amount)

    parts = {
        "on_time": on_time,
        "quality": quality,
        "price_stable": price_stable,
        "loss": loss_score,
    }
    score = 100.0 * sum(parts[k] * w for k, w in RATING_WEIGHTS.items())

    return {
        "supplier_id": supplier.id,
        "name": supplier.name,
        "buy_amount": buy_amount,
        "purchase_count": len(purchases),
        "score": round(score, 2),
        "grade": grade_of(score),
        "breakdown": {
            "on_time": round(on_time, 4),
            "quality": round(quality, 4),
            "price_stable": round(price_stable, 4),
            "loss": round(loss_score, 4),
        },
        "loss_amount": round(loss_amount, 2),
        "credit_days": supplier.credit_days,
        "credit_limit": supplier.credit_limit,
    }


def payables_aging(
    session: Session, shop_id: int, as_of: date | None = None
) -> dict[str, Any]:
    """应付账龄结构 + 逾期清单。

    账龄按**到期日之后过了多久**算（逾期天数），没到期的按"还有几天到期"排序。
    """
    as_of = as_of or date.today()
    rows = session.scalars(
        select(Payable).where(Payable.shop_id == shop_id).order_by(Payable.due_date)
    ).all()
    suppliers = {
        s.id: s for s in session.scalars(select(Supplier).where(Supplier.shop_id == shop_id)).all()
    }

    buckets: dict[str, dict[str, float]] = {
        name: {"amount": 0.0, "count": 0.0} for name, _lo, _hi in AGING_BUCKETS
    }
    overdue_total = 0.0
    outstanding_total = 0.0
    overdue_items: list[dict[str, Any]] = []
    upcoming: list[dict[str, Any]] = []

    for row in rows:
        outstanding = row.outstanding
        if outstanding <= 0.005:          # 已结清的不进账龄
            continue
        outstanding_total += outstanding
        overdue_days = (as_of - row.due_date).days
        age = max(0, overdue_days)
        for name, lo, hi in AGING_BUCKETS:
            if lo is None:                       # "未到期"：overdue_days <= 0
                if overdue_days <= 0:
                    buckets[name]["amount"] += outstanding
                    buckets[name]["count"] += 1
                    break
            elif age >= lo and (hi is None or age <= hi):
                buckets[name]["amount"] += outstanding
                buckets[name]["count"] += 1
                break
        item = {
            "payable_id": row.id,
            "supplier_id": row.supplier_id,
            "supplier_name": suppliers[row.supplier_id].name
            if row.supplier_id in suppliers
            else str(row.supplier_id),
            "due_date": row.due_date.isoformat(),
            "amount": round(outstanding, 2),
            "overdue_days": overdue_days,
        }
        if overdue_days > 0:
            overdue_total += outstanding
            overdue_items.append(item)
        else:
            item["days_left"] = -overdue_days
            upcoming.append(item)

    overdue_items.sort(key=lambda x: x["overdue_days"], reverse=True)
    upcoming.sort(key=lambda x: x["days_left"])

    supplier_ids = {row.supplier_id for row in rows if row.outstanding > 0.005}
    return {
        "as_of": as_of.isoformat(),
        "supplier_count": len(supplier_ids),
        "outstanding_total": round(outstanding_total, 2),
        "overdue_total": round(overdue_total, 2),
        "overdue_count": len(overdue_items),
        "aging": [
            {
                "bucket": name,
                "amount": round(buckets[name]["amount"], 2),
                "count": int(buckets[name]["count"]),
            }
            for name, _lo, _hi in AGING_BUCKETS
        ],
        "overdue_items": overdue_items,
        "upcoming_items": upcoming[:5],
        "sentence": _aging_sentence(supplier_ids, outstanding_total, upcoming, overdue_items),
    }


def _aging_sentence(
    supplier_ids: set[int],
    outstanding_total: float,
    upcoming: list[dict[str, Any]],
    overdue: list[dict[str, Any]],
) -> str | None:
    """任务书 §4 验收要的那句话：「欠 3 家共 X 元，A 家还有 5 天到期，C 家已逾期 12 天」。"""
    if not supplier_ids:
        return None
    parts = [f"欠 {len(supplier_ids)} 家共 {outstanding_total:,.0f} 元"]
    if upcoming:
        first = upcoming[0]
        parts.append(f"{first['supplier_name']}还有 {first['days_left']} 天到期")
    if overdue:
        worst = overdue[0]
        parts.append(f"{worst['supplier_name']}已逾期 {worst['overdue_days']} 天")
    return "，".join(parts) + "。"


def supplier_concentration(
    session: Session, shop_id: int, start: date, end: date
) -> dict[str, Any]:
    """供应商集中度：TOP1 占比。太集中 = 断供风险，太散 = 议价弱。"""
    rows = session.scalars(
        select(Purchase).where(
            Purchase.shop_id == shop_id,
            Purchase.purchase_date >= start,
            Purchase.purchase_date <= end,
        )
    ).all()
    total = sum(float(r.amount or 0.0) for r in rows)
    buckets: dict[int | None, float] = {}
    for row in rows:
        buckets[row.supplier_id] = buckets.get(row.supplier_id, 0.0) + float(row.amount or 0.0)
    suppliers = {
        s.id: s.name for s in session.scalars(select(Supplier).where(Supplier.shop_id == shop_id)).all()
    }
    items = [
        {
            "supplier_id": sid,
            "supplier_name": suppliers.get(sid, "未登记供应商") if sid else "未登记供应商",
            "amount": round(amount, 2),
            "share": round(amount / total, 6) if total else 0.0,
        }
        for sid, amount in buckets.items()
    ]
    items.sort(key=lambda x: x["amount"], reverse=True)
    return {
        "total": round(total, 2),
        "top1_share": items[0]["share"] if items else None,
        "items": items,
    }


def payables_for_shop(
    session: Session, shop_id: int, start: date, end: date
) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(Payable)
        .where(
            Payable.shop_id == shop_id,
            Payable.biz_date >= start,
            Payable.biz_date <= end,
        )
        .order_by(Payable.biz_date)
    ).all()
    suppliers = {
        s.id: s for s in session.scalars(select(Supplier).where(Supplier.shop_id == shop_id)).all()
    }
    return [
        {
            "id": r.id,
            "supplier_id": r.supplier_id,
            "supplier_name": suppliers[r.supplier_id].name
            if r.supplier_id in suppliers
            else str(r.supplier_id),
            "biz_date": r.biz_date.isoformat(),
            "due_date": r.due_date.isoformat(),
            "amount": round(float(r.amount or 0.0), 2),
            "discount_qty": float(r.discount_qty or 0.0),
            "discount_amount": float(r.discount_amount or 0.0),
            "paid_amount": float(r.paid_amount or 0.0),
            "payable_amount": r.payable_amount,
            "outstanding": r.outstanding,
        }
        for r in rows
    ]


def top_expensive_goods(session: Session, shop_id: int, limit: int = 20) -> list[Goods]:
    """按 ABC 里"金额前 20%"挑 A 类候选（这里用商品单价×采购次数近似，够用）。"""
    rows = session.scalars(
        select(Goods).where(Goods.shop_id == shop_id, Goods.enabled == 1)
    ).all()
    return sorted(rows, key=lambda g: float(g.unit_price or 0.0), reverse=True)[:limit]
