"""BOM / 成本卡与差异分析（M5-0）。

规格：v1.0 §3（三条修正）+ docs/05 §3。

## 这一模块存在的理由

M4 的账面公式是 `上期实盘 + Σ入库 − Σ报损` —— **少了"领用"**，
所以差异里混着"用掉的"和"丢掉的"，分不清。

本轮把"领用"按**标准用量**登记（任务书只授权建 `bom`，没有"领料单"表）：

    理论领用 = 卖出份数 × 标准毛料用量(= 标准净用量 ÷ 出成率)

于是：

    账面 = 上期实盘 + Σ入库 − Σ理论领用 − Σ报损
    实盘 − 账面 = "实际用掉的 − 应该用掉的" + 丢失 = **跑冒滴漏**

**这就是"差异变纯"**：不再把正常情况下用掉的料算成"丢的"。

## 差异为什么必须拆量差/价差

总差异 500 元，可能是"厨房浪费 500"（内部管控），也可能是"鸡蛋涨价 500"（外部市场）。
**不拆就不知道该找谁。** 量差找厨房、价差找采购。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Bom, BomVariance, Goods, Stocktake

#: ABC 抽盘周期（天）：A 每周、B 每月、C 每季。只盘 A 类能抓 80% 差异。
#: 不降频的话老板第三天就放弃 —— 这是"让差异分析能落地"的关键，不是偷懒。
ABC_CYCLE_DAYS = {"A": 7, "B": 30, "C": 90}


def gross_qty(std_qty_net: float, yield_rate: float | None) -> float:
    """标准毛料用量 = 净用量 ÷ 出成率。

    出成率缺失或非法（≤0 或 >1）时按 1.0 处理并**由调用方负责提示** ——
    直接当 1.0 会让标准成本算低，所以路由层会给出告警。
    """
    rate = float(yield_rate or 1.0)
    if rate <= 0 or rate > 1.0:
        rate = 1.0
    return round(float(std_qty_net) / rate, 6)


def build_bom_row(session: Session, *, shop_id: int, dish_name: str, goods_id: int,
                  std_qty_net: float, yield_rate: float, unit: str | None = None,
                  std_price: float | None = None, note: str | None = None) -> Bom:
    """建/更新一条成本卡。`std_qty_gross` 由后端算，不接受调用方传入。

    ⚠️ **单位红线（M5 自查审查项 E1 加的）**：成本卡的用量单位必须等于商品的
    **库存单位**。行业里把"采购单位 + 核算单位"都列为标准成本卡的必备字段，
    原文理由是"如果系统按干品计、厨房按泡发后计，理论成本和实际领料必然无法对齐"。

    我们的账面库存是按库存单位累计的，理论领用要直接减进去 ——
    单位一旦错配，`账面 − Σ理论领用` 就是在两个单位之间做减法，数字必然错。
    所以这里**拒绝**不一致的单位，而不是悄悄存下来。
    """
    goods = session.get(Goods, goods_id)
    if goods is None:
        raise ValueError(f"商品 {goods_id} 不存在")
    if unit and goods.unit and unit.strip() != goods.unit.strip():
        raise ValueError(
            f"成本卡的用量单位「{unit}」和商品「{goods.name}」的库存单位"
            f"「{goods.unit}」不一致 —— 理论领用要直接减进账面库存，"
            f"单位不同会算错。请把用量换算成「{goods.unit}」再录，"
            f"或者先改商品的库存单位。"
        )
    price = float(std_price) if std_price is not None else float(goods.unit_price or 0.0)

    existing = session.scalars(
        select(Bom).where(
            Bom.shop_id == shop_id,
            Bom.dish_name == dish_name,
            Bom.goods_id == goods_id,
            Bom.enabled == 1,
        )
    ).first()
    if existing is not None:
        # 改配方 = 新版本，不篡改历史（与"事实不可变"同一个道理）
        existing.enabled = 0
        version = int(existing.version) + 1
    else:
        version = 1

    row = Bom(
        shop_id=shop_id,
        dish_name=dish_name,
        goods_id=goods_id,
        std_qty_net=float(std_qty_net),
        yield_rate=float(yield_rate),
        std_qty_gross=gross_qty(std_qty_net, yield_rate),
        unit=unit or goods.unit,
        std_price=price,
        abc_class=goods.abc_class or "C",
        version=version,
        effective_from=date.today(),
        enabled=1,
        note=note,
    )
    session.add(row)
    session.flush()
    return row


def actual_usage_from_stock(
    session: Session, shop_id: int, goods_id: int, biz_date: date
) -> float | None:
    """从盘点反推**实际耗用**，让老板少录一个数。

        实际耗用 = 上一次实盘 + Σ入库 − 本次实盘 − Σ报损

    ⚠️ **两个坑，都踩过**（M5 自查第二轮 G2）：

    1. **期初必须是"上一次"盘点，不是"最早一次"。**
       如果拿最早那次当期初，3 月 20 日算出来的就是"3 月 1 日到 3 月 20 日
       累计耗用"，却要和"3 月 20 日一天的理论耗用"比 —— 量差被虚高，
       而且前几个周期已经算过的耗用又被算了一遍。
    2. **不能用账面库存反推**：账面已经减掉了"理论领用"（`compute_book_qty`），
       拿它来算实际耗用会自我循环（理论领用越多 → 账面越小 → 实际耗用越大 → 量差越大）。
       必须用入库/报损这些**不依赖 BOM 的原始单据**来推。

    返回 None = 推不出来（不足两次盘点），调用方要提示老板去盘。
    """
    from .stock_service import sum_receipts, sum_scraps

    takes = session.scalars(
        select(Stocktake)
        .where(
            Stocktake.shop_id == shop_id,
            Stocktake.goods_id == goods_id,
            Stocktake.take_date <= biz_date,
        )
        .order_by(Stocktake.take_date.desc(), Stocktake.id.desc())
        .limit(2)
    ).all()
    if len(takes) < 2:
        return None
    closing = float(takes[0].actual_qty)          # 最近一次（≤ biz_date）
    opening = float(takes[1].actual_qty)          # 上一次
    start = takes[1].take_date
    inbound = sum_receipts(session, shop_id, goods_id, start, biz_date)
    scrapped = sum_scraps(session, shop_id, goods_id, start, biz_date)
    return round(opening + inbound - closing - scrapped, 6)


def record_variance(
    session: Session,
    *,
    shop_id: int,
    biz_date: date,
    dish_name: str,
    goods_id: int,
    sold_qty: int,
    actual_usage: float | None = None,
    actual_price: float | None = None,
) -> BomVariance:
    """记一条 BOM 差异：理论用量（= 领用）自动算，量差/价差分别算。

    **实际用量不填就自己推**（M5 自查审查项 F2）：本来老板要额外录"今天用了多少"，
    那是**新增录入动作**，违反项目硬约束（每日新增 ≤ 30 秒）。
    而"实际耗用"本来就能从盘点算出来 —— 能算的就不该让老板填。
    """
    bom = session.scalars(
        select(Bom).where(
            Bom.shop_id == shop_id,
            Bom.dish_name == dish_name,
            Bom.goods_id == goods_id,
            Bom.enabled == 1,
        )
    ).first()
    if bom is None:
        raise ValueError(f"没有找到「{dish_name} × 商品 {goods_id}」的成本卡，先建配方再算差异")

    if actual_usage is None:
        actual_usage = actual_usage_from_stock(session, shop_id, goods_id, biz_date)
        if actual_usage is None:
            raise ValueError(
                "没填实际用量，而盘点数据也不够（需要至少两次盘点才能反推耗用）。"
                "要么补一次盘点，要么直接把实际用量填上。"
            )

    # M5 自查 G3：一天一条，**重复保存 = 覆盖**（与盘点单同一套语义）。
    # 不覆盖的话，std_usage 会直接翻倍减进账面库存 —— 账面凭空少掉两份料，
    # 差异率虚高，而且这个错极难被发现（数字看着都"正常"）。
    existing = session.scalars(
        select(BomVariance).where(
            BomVariance.shop_id == shop_id,
            BomVariance.biz_date == biz_date,
            BomVariance.dish_name == dish_name,
            BomVariance.goods_id == goods_id,
        )
    ).first()
    if existing is not None:
        row = existing
    else:
        row = BomVariance(
            shop_id=shop_id,
            biz_date=biz_date,
            dish_name=dish_name,
            goods_id=goods_id,
        )
        session.add(row)

    std_usage = round(float(sold_qty) * float(bom.std_qty_gross), 6)
    std_price = float(bom.std_price or 0.0)
    price = float(actual_price) if actual_price is not None else std_price

    # 量差找厨房：多用了多少 × 标准价
    qty_diff = round(float(actual_usage) - std_usage, 6)
    # 价差找采购：贵了多少 × 实际用量
    qty_amount = round(qty_diff * std_price, 6)
    price_amount = round((price - std_price) * float(actual_usage), 6)

    row.sold_qty = int(sold_qty)
    row.std_usage = std_usage
    row.actual_usage = float(actual_usage)
    row.std_price = std_price
    row.actual_price = price
    row.qty_variance = qty_diff
    row.qty_variance_amount = qty_amount
    row.price_variance_amount = price_amount
    row.abc_class = bom.abc_class or "C"
    session.flush()
    return row
    return row


def theoretical_usage(session: Session, shop_id: int, start: date, end: date) -> tuple[float, float]:
    """区间内的**理论领用**（数量按商品汇总不了单位，所以返回"金额口径"）。

    返回 `(理论领用金额, 理论领用份数次)`。

    ⚠️ 数量口径：不同商品单位不同（斤/L/个），**不能相加**。
    所以进账面公式时用**按标准价折算的金额**，单位换算交给各商品自己的 BOM。
    """
    rows = session.scalars(
        select(BomVariance).where(
            BomVariance.shop_id == shop_id,
            BomVariance.biz_date >= start,
            BomVariance.biz_date <= end,
        )
    ).all()
    amount = 0.0
    qty = 0.0
    for row in rows:
        amount += float(row.std_usage or 0.0) * float(row.std_price or 0.0)
        qty += float(row.std_usage or 0.0)
    return round(amount, 6), round(qty, 6)


def usage_by_goods(
    session: Session, shop_id: int, goods_id: int, start: date, end: date
) -> float:
    """某个商品在区间内的理论领用**数量**（账面推算要按货品算，这里保持单位一致）。"""
    total = session.scalar(
        select(func.coalesce(func.sum(BomVariance.std_usage), 0.0)).where(
            BomVariance.shop_id == shop_id,
            BomVariance.goods_id == goods_id,
            BomVariance.biz_date >= start,
            BomVariance.biz_date <= end,
        )
    )
    return round(float(total or 0.0), 6)


def variance_summary(session: Session, shop_id: int, start: date, end: date) -> dict[str, Any]:
    """量差 / 价差汇总 —— 分别给出，因为要分别去找厨房和采购。"""
    rows = session.scalars(
        select(BomVariance).where(
            BomVariance.shop_id == shop_id,
            BomVariance.biz_date >= start,
            BomVariance.biz_date <= end,
        )
    ).all()
    qty_amount = round(sum(float(r.qty_variance_amount or 0.0) for r in rows), 2)
    price_amount = round(sum(float(r.price_variance_amount or 0.0) for r in rows), 2)
    by_dish: dict[str, dict[str, float]] = {}
    for row in rows:
        item = by_dish.setdefault(row.dish_name, {"qty": 0.0, "price": 0.0})
        item["qty"] += float(row.qty_variance_amount or 0.0)
        item["price"] += float(row.price_variance_amount or 0.0)

    return {
        "has_data": bool(rows),
        "qty_variance_amount": qty_amount,
        "price_variance_amount": price_amount,
        "total_variance_amount": round(qty_amount + price_amount, 2),
        "dishes": [
            {
                "dish_name": name,
                "qty_variance_amount": round(v["qty"], 2),
                "price_variance_amount": round(v["price"], 2),
            }
            for name, v in sorted(
                by_dish.items(), key=lambda kv: abs(kv[1]["qty"] + kv[1]["price"]), reverse=True
            )
        ],
        "conclusion": _variance_conclusion(qty_amount, price_amount),
    }


def _variance_conclusion(qty_amount: float, price_amount: float) -> str | None:
    total = qty_amount + price_amount
    if abs(total) < 0.005:
        return None
    if abs(qty_amount) >= abs(price_amount):
        return (
            f"标准成本外的差额里，{abs(qty_amount):,.0f} 元是**用多了**（量差，厨房环节），"
            f"{abs(price_amount):,.0f} 元是**买贵了**（价差，采购环节）—— 主要问题在厨房。"
        )
    return (
        f"标准成本外的差额里，{abs(qty_amount):,.0f} 元是用多了（量差），"
        f"{abs(price_amount):,.0f} 元是买贵了（价差，采购环节）—— 主要问题在采购。"
    )


def counting_due(session: Session, shop_id: int, as_of: date | None = None) -> dict[str, Any]:
    """本期待盘清单（ABC 抽盘）。

    A 类每周、B 类每月、C 类每季。不要求每天全盘 ——
    **只盘 A 类能抓 80% 的差异**，而且老板才坚持得下去。
    """
    as_of = as_of or date.today()
    goods = session.scalars(
        select(Goods).where(Goods.shop_id == shop_id, Goods.enabled == 1)
    ).all()
    last_take: dict[int, date] = {}
    for row in session.scalars(
        select(Stocktake).where(Stocktake.shop_id == shop_id).order_by(Stocktake.take_date)
    ).all():
        last_take[row.goods_id] = row.take_date

    items: list[dict[str, Any]] = []
    for g in goods:
        cycle = ABC_CYCLE_DAYS.get(g.abc_class or "C", 90)
        last = last_take.get(g.id)
        days_since = (as_of - last).days if last else None
        due = days_since is None or days_since >= cycle
        items.append(
            {
                "goods_id": g.id,
                "code": g.code,
                "name": g.name,
                "spec": g.spec,
                "abc_class": g.abc_class or "C",
                "cycle_days": cycle,
                "last_take_date": last.isoformat() if last else None,
                "days_since_last": days_since,
                "due": due,
            }
        )
    items.sort(key=lambda x: (not x["due"], {"A": 0, "B": 1, "C": 2}[x["abc_class"]]))
    due_items = [i for i in items if i["due"]]
    return {
        "as_of": as_of.isoformat(),
        "due_count": len(due_items),
        "items": items,
        "note": (
            "A 类每周盘、B 类每月、C 类每季。只盘 A 类能抓到约 80% 的差异 —— "
            "每天全盘不现实，抽盘才落得了地。"
        ),
    }
