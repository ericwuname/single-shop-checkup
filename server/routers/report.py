"""报告输出（施工单 3.2 / server/routers/report.py）。

产出三样东西：
  1. **指标卡** —— 每个指标的值 + 同行区间对照（engine.compute 的结果 + benchmarks）
  2. **大白话结论** —— 严格三行：第一行数字 / 第二行判断 / 第三行一个动作（施工单 1.4）
  3. **环比段 trend** —— 与上一等长区间对比（M2-3）

硬约束：结论里**绝不出现财务术语**（变动成本 / 边际贡献 / 保本点 …），
由 tests/test_daily.py::test_conclusion_has_no_jargon 守着。

------------------------------------------------------------------ M2 增补
* **M2-2 话术分档**：按 `days_span` 分三档（≤7 日/周、8–90 月/季、>90 年度）。
  日档话术是"人话模板"，月/年档由 `benchmarks.action_for_tier` 机械改写得到。
  年度档第三行的硬判据：**不得出现"明天"**。
* **M2-3 环比**：取"上一等长区间"独立跑一遍引擎再比 —— 两个区间各自先 Σ 原子再算比率，
  天然满足"禁止对日率取平均"的纪律（这是最容易图省事写错的地方，所以有专测）。
* **M2-4 保本自比对**：保本额没有同行区间，只能和自己比。年度档第二行改说这件事。
  **刻意保持严格三行**：施工单 §1.4 的"只有三行"是硬约束，所以 M2-4 的句子是替换第二行，
  不是追加第四行。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import benchmarks as bm
from ..db import get_db
from ..goods_master import CATEGORY_SEEDS
from ..metrics_engine import MetricsEngine, month_first_day, month_last_day
from ..models import DimCalendar, FactDaily, FieldDef, Goods, MetricDef, Purchase, Shift, Stocktake
from ..schemas import (
    BenchmarkVerdict,
    ConclusionOut,
    MetricResult,
    MonthlySeriesOut,
    PeriodOut,
    ReportOut,
    ShopOut,
    TrendItem,
)
from .daily import require_shop

router = APIRouter(prefix="/api/report", tags=["report"])

#: 少于这个天数就在结论里挂"仅供参考"（施工单 3.6）
MIN_DAYS_FOR_FULL_CONFIDENCE = 7

#: 报告里指标的展示顺序：核心指标在前，支撑量次之，原始数字最后补齐
#: 核心 5 → 7：M3-0 授权加 labor_cost_rate_full，M3-3 授权加 stock_variance_rate
CORE_ORDER = (
    "labor_cost_rate",
    "labor_cost_rate_full",
    "efficiency",
    "avg_ticket",
    "food_cost_rate",
    "breakeven_daily",
    "stock_variance_rate",
)
SUPPORT_ORDER = (
    "labor_cost",
    "hours",
    "labor_m",
    "labor_m_full",
    # M3-2 隐性成本
    "hidden_cost_m",
    "real_profit_m",
    "book_profit_m",
    # M3-3 账实差异
    "stock_variance_amount",
    "scrap_amount",
    "stock_book_amount",
    "stock_actual_amount",
    # M3-0 月口径
    "revenue_m",
    "purchase_m",
)
RAW_ORDER = (
    "revenue",
    "purchase",
    "orders",
    "rent_m",
    "utility_m",
    "gross_margin",
    "full_time_headcount",
    "full_time_monthly",
    "social_insurance_m",
    "housing_fund_m",
    "welfare_m",
    "staff_meal_m",
    "tableware_m",
    "repair_m",
    "cash_diff_m",
    "returned_m",
)

#: 环比段要看哪些 code（M2-3）。revenue 是字段不是指标，但老板最需要看它的涨跌。
TREND_CODES = (
    "food_cost_rate",
    "labor_cost_rate",
    "efficiency",
    "avg_ticket",
    "breakeven_daily",
    "revenue",
)


# --------------------------------------------------------------------------- #
# 格式化
# --------------------------------------------------------------------------- #
RATIO_UNITS = ("ratio", "比例")


def format_value(md: MetricDef | FieldDef | None, value: float | None) -> str:
    """比率 → 百分数（1 位小数）；其余 → 整数优先、否则 2 位小数。

    注意：**不拼单位**。单位由 plain_text 模板承载（如「人工花了 {value} 元」），
    这里再拼一次就会出现"38 小时 小时"。
    """
    if value is None:
        return "--"
    if md is not None and (md.unit or "") in RATIO_UNITS:
        return f"{value:.1%}"
    if abs(value - round(value)) < 0.005:
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def _trend_fmt(md: MetricDef | FieldDef | None, value: float | None) -> str:
    """环比句里的数字：比率用整数百分比（老板读"30% → 41%"比"30.0%"顺眼）。"""
    if value is None:
        return "--"
    if md is not None and (md.unit or "") in RATIO_UNITS:
        return f"{value:.0%}"
    return f"{value:,.0f}"


def render_sentence(md: MetricDef | None, value: float | None) -> str | None:
    if md is None or value is None:
        return None
    template = md.plain_text
    if not template:
        return f"{md.name or md.code} {format_value(md, value)}"
    return template.replace("{value}", format_value(md, value))


def _num_text(md: MetricDef | FieldDef | None, value: float | None) -> str | None:
    return format_value(md, value) if value is not None else None


# --------------------------------------------------------------------------- #
# 观察项
# --------------------------------------------------------------------------- #
def _observations(session: Session, shop_id: int, start: date, end: date) -> dict[str, Any]:
    rows = session.scalars(
        select(FactDaily)
        .where(
            FactDaily.shop_id == shop_id,
            FactDaily.period_type == "day",
            FactDaily.stat_date >= start,
            FactDaily.stat_date <= end,
        )
        .order_by(FactDaily.stat_date)
    ).all()

    cal = {
        c.date: c
        for c in session.scalars(
            select(DimCalendar).where(
                DimCalendar.date >= start.isoformat(), DimCalendar.date <= end.isoformat()
            )
        ).all()
    }

    daily: list[dict[str, Any]] = []
    weekday_rev: list[float] = []
    weekend_rev: list[float] = []

    for row in rows:
        key = row.stat_date.isoformat()
        entry = cal.get(key)
        is_weekend = bool(entry.is_weekend) if entry else row.stat_date.weekday() >= 5
        label = entry.label if entry else key
        daily.append(
            {
                "date": key,
                "label": label,
                "revenue": row.revenue,
                "purchase": row.purchase,
                "orders": row.orders,
                "is_weekend": is_weekend,
            }
        )
        if row.revenue is not None:
            (weekend_rev if is_weekend else weekday_rev).append(float(row.revenue))

    revenues = [d["revenue"] for d in daily if d["revenue"] is not None]
    best = max(daily, key=lambda d: d["revenue"] or -1, default=None)
    worst = min(
        [d for d in daily if d["revenue"] is not None],
        key=lambda d: d["revenue"],
        default=None,
    )

    return {
        "daily": daily,
        "weekday_avg_revenue": round(sum(weekday_rev) / len(weekday_rev), 2) if weekday_rev else None,
        "weekend_avg_revenue": round(sum(weekend_rev) / len(weekend_rev), 2) if weekend_rev else None,
        "total_revenue": round(sum(revenues), 2) if revenues else None,
        "daily_avg_revenue": round(sum(revenues) / len(revenues), 2) if revenues else None,
        "best_day": {"date": best["date"], "label": best["label"], "revenue": best["revenue"]}
        if best and best["revenue"] is not None
        else None,
        "worst_day": {"date": worst["date"], "label": worst["label"], "revenue": worst["revenue"]}
        if worst
        else None,
    }


def _lookup_def(
    code: str, metric_defs: dict[str, MetricDef], field_defs: dict[str, FieldDef]
) -> MetricDef | FieldDef | None:
    return metric_defs.get(code) or field_defs.get(code)


# --------------------------------------------------------------------------- #
# M2-3 环比
# --------------------------------------------------------------------------- #
def _direction(delta: float | None, previous: float | None, is_ratio: bool) -> str:
    """升 / 平 / 降。比率用绝对阈值（0.1 个百分点），其余用相对阈值（0.5%）。"""
    if delta is None:
        return "平"
    if is_ratio:
        eps = 0.001
    else:
        eps = abs(previous) * 0.005 if previous else 0.001
    if delta > eps:
        return "升"
    if delta < -eps:
        return "降"
    return "平"


_VERB = {"升": "在涨", "降": "在降", "平": "没变"}


def build_trend(
    current: dict[str, float | None],
    previous: dict[str, float | None],
    *,
    metric_defs: dict[str, MetricDef],
    field_defs: dict[str, FieldDef],
    tier: str,
) -> list[TrendItem]:
    """与上一等长区间对比。

    `current` / `previous` 是**两个区间各自独立跑一遍引擎**的结果 ——
    所以每一边都是"先 Σ 原子再算比率"，不存在对日率取平均的可能。
    """
    items: list[TrendItem] = []
    for code in TREND_CODES:
        md = _lookup_def(code, metric_defs, field_defs)
        if md is None:
            continue
        cur, prev = current.get(code), previous.get(code)
        delta = (cur - prev) if (cur is not None and prev is not None) else None
        is_ratio = (md.unit or "") in RATIO_UNITS
        direction = _direction(delta, prev, is_ratio)

        sentence = None
        if delta is not None:
            name = getattr(md, "name", None) or getattr(md, "label", None) or code
            sentence = (
                f"{name}{bm.period_word(tier)}{_VERB[direction]}"
                f"（{_trend_fmt(md, prev)} → {_trend_fmt(md, cur)}）"
            )

        items.append(
            TrendItem(
                code=code,
                name=(getattr(md, "name", None) or getattr(md, "label", None) or code),
                current=cur,
                previous=prev,
                delta=round(delta, 6) if isinstance(delta, float) else delta,
                direction=direction,
                sentence=sentence,
            )
        )
    return items


def _revenue_pct_change(observations: dict[str, Any]) -> float | None:
    now = observations.get("daily_avg_revenue")
    prev = observations.get("prev_daily_avg_revenue")
    if not now or not prev:
        return None
    return (now - prev) / prev


# --------------------------------------------------------------------------- #
# M3-3 账实差异
# --------------------------------------------------------------------------- #
def _stock_variance(
    session: Session, shop_id: int, start: date, end: date, rate: float | None
) -> dict[str, Any]:
    """把盘点事实整理成"能给老板看"的一段。

    分工说明：**差异率是注册指标**（`stock_variance_rate`，公式在 metric_def 里），
    逐货品明细来自 `stock_service.variance_detail()`（账面库存的唯一实现）——
    这里只负责拼那一句话，不重算任何东西。
    """
    from ..stock_service import variance_detail

    items = variance_detail(session, shop_id, start, end)

    out: dict[str, Any] = {
        "has_data": bool(items),
        "rate": rate,
        "sentence": None,
        "items": items,
        "note": (
            "账面库存 = 上期实盘 + Σ入库 − **Σ理论领用** − Σ报损。"
            "领用按 BOM 的标准用量登记（卖出份数 × 标准毛料用量）—— "
            "所以这里的差异已经是「实际用掉的 − 应该用掉的」，也就是跑冒滴漏（M5-0 起口径变纯）。"
            "⚠️ 它依赖成本卡的准确性：配方没录或录错的品，差异会偏。"
        ),
    }
    if not items:
        return out

    # 差异为 0 就不说话 —— 任务书明确要求"不许制造噪音"。
    # 另外：账面数为 None 的条目（系统推不出来、老板也没自报）不能参与"最严重"的比较，
    # 否则格式化时直接崩 —— 缺账面就说明这张盘点单本身还不完整，先不拿它说话。
    comparable = [i for i in items if i.get("book_qty") is not None]
    if comparable:
        worst = max(comparable, key=lambda x: abs(x["variance_amount"]))
        if abs(worst["variance_amount"]) > 0.005:
            unit = worst["unit"] or ""
            spec = f"（{worst['spec']}）" if worst.get("spec") else ""
            out["sentence"] = (
                f"{worst['goods_name']}{spec} 账面应该剩 {worst['book_qty']:g}{unit}，"
                f"实际只有 {worst['actual_qty']:g}{unit}，"
                f"差 {abs(worst['variance_qty']):g}{unit} ≈ {abs(worst['variance_amount']):,.0f} 元。"
            )
            out["worst"] = worst
    return out


def _payables_summary(session: Session, shop_id: int, as_of: date) -> dict[str, Any]:
    """M5-1：报告要能说出「欠 3 家共 X 元，A 家还有 5 天到期，C 家已逾期 12 天」。

    放在 observations 里而不是结论三行 —— 结论严格三行是门禁，
    而且"欠谁钱"是要单独看的一块，塞进去会挤掉更重要的那句话。
    """
    from ..supplier_service import payables_aging

    return payables_aging(session, shop_id, as_of)


def _bom_variance_summary(
    session: Session, shop_id: int, start: date, end: date
) -> dict[str, Any]:
    """M5-0：量差 / 价差进报告（量差找厨房、价差找采购）。"""
    from ..bom_service import variance_summary

    return variance_summary(session, shop_id, start, end)


# --------------------------------------------------------------------------- #
# M3-6 录入约定防呆（告警，不是报错）
# --------------------------------------------------------------------------- #
def _input_alerts(
    session: Session, shop_id: int, values: dict[str, float | None], end: date
) -> list[dict[str, str]]:
    """系统同时支持两种录入约定，两边都填会重复计算。

    约定 A：全职走**排班**（把月薪折算成时薪，M1 的唯一办法）
    约定 B：全职走**月薪字段**（`full_time_monthly` + 社保公积金福利）

    A + B 同时用 → `labor_m_full` 会把同一笔工钱算两遍。**告警而不是报错**：
    我们不能替老板判断他的实际情况，只能提醒他去确认。
    """
    alerts: list[dict[str, str]] = []
    monthly = float(values.get("full_time_monthly") or 0.0)
    headcount = float(values.get("full_time_headcount") or 0.0)
    if monthly <= 0:
        return alerts

    anchor = month_first_day(end)
    shifts = session.scalars(
        select(Shift).where(
            Shift.shop_id == shop_id,
            Shift.work_date >= anchor,
            Shift.work_date <= month_last_day(anchor),
        )
    ).all()
    if not shifts:
        return alerts

    # 两种识别方式：名字里带"全职"，或时薪正好是折算出来的那个数
    guessed_rate = monthly / (headcount * 8 * 30) if headcount > 0 else None
    suspects = [
        s
        for s in shifts
        if "全职" in (s.worker_name or "")
        or (
            guessed_rate is not None
            and s.hourly_rate is not None
            and abs(float(s.hourly_rate) - guessed_rate) < 0.5
        )
    ]
    if suspects:
        names = "、".join(sorted({s.worker_name for s in suspects})[:3])
        alerts.append(
            {
                "code": "labor_double_count_risk",
                "level": "warn",
                "message": (
                    f"这个月既填了全职月薪 {monthly:,.0f} 元，又给「{names}」记了排班。"
                    f"两样都算会把同一笔工钱算两遍（重复计算）—— 二选一："
                    f"全职走月薪字段，小时工走排班。"
                ),
            }
        )
    return alerts


# --------------------------------------------------------------------------- #
# M2-4 保本自比对
# --------------------------------------------------------------------------- #
def _breakeven_self_compare(
    trend: list[TrendItem], observations: dict[str, Any], tier: str
) -> tuple[str | None, str | None]:
    """保本额没有同行区间，只能和上一期自己比。只在年度档说，避免 7 天报告里添噪音。"""
    if tier != bm.TIER_YEAR:
        return None, None

    be = next((t for t in trend if t.code == "breakeven_daily"), None)
    if be is None or be.current is None or be.previous is None or not be.previous:
        return None, None
    if be.delta is None or be.delta <= 0:
        return None, None

    be_pct = be.delta / be.previous
    rev_pct = _revenue_pct_change(observations)

    if rev_pct is None:
        line2 = (
            f"保本线从 {be.previous:,.0f} 涨到 {be.current:,.0f} 元/天（涨了 {be_pct:.0%}），"
            f"这是每天必须做到的数。"
        )
    else:
        rev_phrase = f"只涨了 {rev_pct:.0%}" if rev_pct >= 0 else f"反而降了 {abs(rev_pct):.0%}"
        tail = "你越做越危险。" if be_pct > rev_pct else "眼下还跟得上。"
        line2 = (
            f"保本线这一年从 {be.previous:,.0f} 涨到 {be.current:,.0f} 元/天（涨了 {be_pct:.0%}），"
            f"而日均营业额{rev_phrase} —— {tail}"
        )

    # 动作行带上"涨了多少"，让年度档的第三行自己也是年份相关的
    # （否则 Y2/Y3 的动作行会一模一样，门禁 4 只靠前两行撑着）
    line3 = (
        f"明年最先要动的是：先压住保本线（这一年涨了 {be.delta:,.0f} 元/天），"
        f"把房租和人工这两块的价钱重新谈一遍。"
    )
    return line2, line3


# --------------------------------------------------------------------------- #
# 三行结论（M2-2 分档）
# --------------------------------------------------------------------------- #
def build_conclusion(
    *,
    values: dict[str, float | None],
    metric_defs: dict[str, MetricDef],
    field_defs: dict[str, FieldDef] | None = None,
    category: str,
    days_span: int,
    days_with_data: int,
    observations: dict[str, Any],
    blocked: list[dict[str, str]],
    trend: list[TrendItem] | None = None,
    stock_variance: dict[str, Any] | None = None,
    input_alerts: list[dict[str, str]] | None = None,
) -> ConclusionOut:
    field_defs = field_defs or {}
    trend = trend or []
    stock_variance = stock_variance or {}
    input_alerts = input_alerts or []
    tier = bm.tier_for(days_span)
    tone = bm.TIERS[tier]

    def md(code: str):
        return metric_defs.get(code)

    def text(code: str) -> str | None:
        return _num_text(md(code), values.get(code))

    warnings: list[str] = []

    # ---------------- 第一行：数字 ----------------
    revenue = text("revenue")
    labor = text("labor_cost")
    labor_rate = text("labor_cost_rate")
    purchase = text("purchase")
    food_rate = text("food_cost_rate")

    if revenue is None:
        line1 = "今天的营业额还没填。先把营业额填上，占多少才算得出来。"
    else:
        parts: list[str] = []
        if days_with_data <= 1:
            parts.append(f"今天营业额 {revenue}")
        else:
            avg = observations.get("daily_avg_revenue")
            head = f"这 {days_with_data} 天营业额 {revenue}"
            if isinstance(avg, (int, float)):
                head += f"（日均 {avg:,.2f}）"
            parts.append(head)

        if labor is not None:
            parts.append(f"人工 {labor}" + (f"（占 {labor_rate}）" if labor_rate else ""))
        if purchase is not None:
            parts.append(f"食材 {purchase}" + (f"（占 {food_rate}）" if food_rate else ""))

        # M2-1 报告口径：两个数一起给，差额就是老板看不见的那笔钱。
        # 只在非日档说 —— labor_m / labor_m_full 都是**月口径**，在单日报告里摆出来会让人误读尺度。
        if tier != bm.TIER_DAY:
            labor_m = values.get("labor_m")
            labor_m_full = values.get("labor_m_full")
            if labor_m is not None and labor_m_full is not None and labor_m_full > labor_m:
                gap = labor_m_full - labor_m
                parts.append(
                    f"按月口径，排班工钱 {labor_m:,.0f}、全口径人工 {labor_m_full:,.0f}，"
                    f"中间差的 {gap:,.0f} 是你没算进去的钱"
                )

        # M3-2 隐性成本：老板以为赚的 vs 实际剩下的。
        # 放在**第一行（数字行）**而不是第二行 —— 它是两个数加一个差额，语义上属于"数字"；
        # 第二行留给"判断"。这样也不会挤掉年度档的保本自比对。
        if tier != bm.TIER_DAY:
            book = values.get("book_profit_m")
            real = values.get("real_profit_m")
            hidden = values.get("hidden_cost_m")
            if book is not None and real is not None and hidden is not None and hidden > 0:
                book_word = "账面赚了" if book >= 0 else "账面亏了"
                real_word = "实际还剩" if real >= 0 else "实际还亏"
                parts.append(
                    f"按月算，{book_word} {abs(book):,.0f}，算上员工餐、餐具、维修、收银差异"
                    f"这些容易被漏掉的，{real_word} {abs(real):,.0f}，中间差 {hidden:,.0f}"
                )

        # 日档沿用逗号（和施工单 §1.4 的样例一致）；长区间用分号分组，因为要摆的口径更多
        separator = "，" if tier == bm.TIER_DAY else "；"
        line1 = separator.join(parts) + "。"

    # ---------------- 第二行：判断 ----------------
    line2: str | None = None
    line3: str | None = None

    # M3-3 账实差异优先级**最高**：它给的是"具体哪样东西少了多少、值多少钱"，
    # 比其他判断都更可动手（任务书称它是本产品的护城河）。
    # 没盘点数据、或差异恰好为 0 时这段是空的，不占位、不制造噪音。
    if stock_variance.get("sentence"):
        line2 = stock_variance["sentence"] + "这笔钱不是行情问题，是流程问题。"
        line3 = (
            "下一次逐样过一遍：是收货没称、还是领用没记、还是被人拿走了 —— "
            "先把这个数压到 1% 以内。"
        )

    if line2 is None:
        line2, line3 = _breakeven_self_compare(trend, observations, tier)

    if line2 is None:
        breakeven = values.get("breakeven_daily")
        daily_avg = observations.get("daily_avg_revenue")
        if breakeven is not None and daily_avg is not None and daily_avg < breakeven:
            line2 = (
                f"{bm.verdict_for_tier('每天要做到', tier)} "
                f"{format_value(md('breakeven_daily'), breakeven)} 元才不亏，"
                f"现在日均 {daily_avg:,.2f}，还差 {breakeven - daily_avg:,.2f} 元。"
            )
            line3 = bm.action_for_tier(
                "把没做到的那几天翻出来看，先找出掉得最多的那天发生了什么。", tier
            )

    if line2 is None:
        for code in bm.VERDICT_PRIORITY:
            verdict = bm.compare(category, code, values.get(code), tier)
            if verdict and verdict["status"] in {"high", "low"}:
                line2 = verdict["text"] or None
                line3 = verdict["action"] or None
                break

    if not line2:
        line2 = tone["default_verdict"]
    if not line3:
        line3 = tone["default_action"]

    # ---------------- 提示 ----------------
    if days_span <= bm.TIER_DAY_MAX:
        if days_with_data < MIN_DAYS_FOR_FULL_CONFIDENCE:
            warnings.append(f"数据不足 {MIN_DAYS_FOR_FULL_CONFIDENCE} 天，仅供参考。")
    elif days_with_data < days_span * 0.5:
        warnings.append(
            f"这段区间 {days_span} 天里只有 {days_with_data} 天有数据，仅供参考。"
        )

    if values.get("revenue") is None:
        warnings.append("填了营业额才能算占比。")
    if values.get("hours") is None:
        warnings.append("还没记工时，人工和人效算不出来。")
    for item in blocked:
        name = (md(item["code"]).name if md(item["code"]) else item["code"]) or item["code"]
        warnings.append(f"{name}还算不出，缺：{item['missing']}。")
    # M3-6 防呆：告警走 warnings（老板看得见），不是报错
    for alert in input_alerts:
        warnings.append(alert["message"])

    return ConclusionOut(lines=[line1, line2, line3], warnings=warnings)


# --------------------------------------------------------------------------- #
# M4-9 · 异常清单（按金额降序）
# --------------------------------------------------------------------------- #
#: 比率型指标的"折算成钱"用哪个营业额口径。
#: 日/区间口径的指标用区间营业额；月口径的（全口径人力）用月化营业额 —— 混着用会虚高。
_ANOMALY_REVENUE_SCOPE = {
    "food_cost_rate": "revenue",
    "labor_cost_rate_full": "revenue_m_avg",
    "labor_cost_rate": "revenue",
}


def build_anomalies(
    values: dict[str, float | None],
    metric_defs: dict[str, MetricDef],
    field_defs: dict[str, FieldDef],
    category: str,
    tier: str,
    stock_sentence: str | None = None,
    stock_amount: float | None = None,
) -> list[dict[str, Any]]:
    """把"越线的指标"折成钱，按金额降序 —— 老板先看钱最多的那个。

    折钱规则（写出来是为了可核对）：
      * **比率型**（食材率/人力率）：|超出边界的幅度| × 对应口径的营业额
      * **人效**：把"差多少元/工时"乘回总工时
      * **账实差异**：直接用差异金额
    """
    out: list[dict[str, Any]] = []

    for code, md in metric_defs.items():
        value = values.get(code)
        if value is None or not md.benchmark_key:
            continue
        bench = bm.benchmark_for(category, md.benchmark_key)
        if bench is None or bench.get("low") is None or bench.get("high") is None:
            continue

        low, high = float(bench["low"]), float(bench["high"])
        if value > high:
            status, bound = "high", high
        elif value < low:
            status, bound = "low", low
        else:
            continue

        gap = abs(float(value) - bound)
        if (md.unit or "") in RATIO_UNITS:
            rev = values.get(_ANOMALY_REVENUE_SCOPE.get(code, "revenue"))
            amount = gap * float(rev) if rev else 0.0
        elif code == "efficiency":
            hours = values.get("hours")
            amount = gap * float(hours) if hours else 0.0
        else:
            amount = gap

        name = md.name or (field_defs.get(code).label if field_defs.get(code) else code)
        out.append(
            {
                "code": code,
                "name": name,
                "status": status,
                "display": format_value(md, value),
                "amount": round(amount, 2),
                "sentence": bm.compare(category, md.benchmark_key, value, tier)["text"]
                if bm.compare(category, md.benchmark_key, value, tier)
                else None,
            }
        )

    # 账实差异：它不是基准越线，是"钱直接少了"
    if stock_sentence and stock_amount:
        out.append(
            {
                "code": "stock_variance",
                "name": "账实差异",
                "status": "warn",
                "display": f"{abs(stock_amount):,.0f} 元",
                "amount": round(abs(stock_amount), 2),
                "sentence": stock_sentence,
            }
        )

    out.sort(key=lambda x: x["amount"], reverse=True)
    return out


# --------------------------------------------------------------------------- #
# 组装报告
# --------------------------------------------------------------------------- #
def assemble_report(session: Session, shop_id: int, start: date, end: date) -> ReportOut:
    shop = require_shop(session, shop_id)
    if start > end:
        start, end = end, start

    days_span = (end - start).days + 1
    tier = bm.tier_for(days_span)

    engine = MetricsEngine(session)
    values = engine.compute(shop.id, start, end)
    session.commit()

    days_rows = session.scalars(
        select(FactDaily).where(
            FactDaily.shop_id == shop.id,
            FactDaily.period_type == "day",
            FactDaily.stat_date >= start,
            FactDaily.stat_date <= end,
        )
    ).all()
    days_with_data = len(days_rows)

    # ---- M2-3：上一等长区间 ----
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days_span - 1)
    has_prev = bool(
        session.scalar(
            select(func.count())
            .select_from(FactDaily)
            .where(
                FactDaily.shop_id == shop.id,
                FactDaily.period_type == "day",
                FactDaily.stat_date >= prev_start,
                FactDaily.stat_date <= prev_end,
            )
        )
    )

    observations = _observations(session, shop.id, start, end)
    trend: list[TrendItem] = []
    if has_prev:
        # persist=False：上一期只是拿来比，不该污染 metric_value 缓存
        prev_values = engine.compute(shop.id, prev_start, prev_end, persist=False)
        prev_obs = _observations(session, shop.id, prev_start, prev_end)
        observations["prev_daily_avg_revenue"] = prev_obs.get("daily_avg_revenue")
        observations["prev_period"] = {
            "start": prev_start.isoformat(),
            "end": prev_end.isoformat(),
        }
        trend = build_trend(
            values,
            prev_values,
            metric_defs=engine.metric_defs,
            field_defs=engine.field_defs,
            tier=tier,
        )

    # ---- M2-1：两个人工口径的差额放进观测项 ----
    labor_m = values.get("labor_m")
    labor_m_full = values.get("labor_m_full")
    observations["labor"] = {
        "month_anchor": month_first_day(end).strftime("%Y-%m"),
        "labor_m_direct": labor_m,
        "labor_m_full": labor_m_full,
        "gap": (labor_m_full - labor_m)
        if (labor_m is not None and labor_m_full is not None)
        else None,
        "note": (
            "两个口径都是**月口径**（取区间结束日所在月）。"
            "差额 = 社保 + 公积金 + 福利 + 未进排班的全职月薪，即老板通常没算进去的那笔钱。"
        ),
    }

    # ---- 指标卡 ----
    ordered_codes: list[str] = [c for c in CORE_ORDER if c in values]
    ordered_codes += [c for c in SUPPORT_ORDER if c in values]
    ordered_codes += [c for c in RAW_ORDER if c in values]
    ordered_codes += [c for c in sorted(values) if c not in ordered_codes]

    metrics: list[MetricResult] = []
    for code in ordered_codes:
        md = engine.metric_defs.get(code)
        fd = engine.field_defs.get(code)
        value = values.get(code)
        # 原子字段不在 metric_def 里（它们由 field_def 描述），名字/单位要从 field_def 取
        name = (md.name if md else None) or (fd.label if fd else None) or code
        unit = (md.unit if md else None) or (fd.unit if fd else None)
        verdict = bm.compare(shop.category, md.benchmark_key, value, tier) if md else None
        metrics.append(
            MetricResult(
                code=code,
                name=name,
                value=value,
                display=format_value(md or fd, value),
                unit=unit,
                layer=md.layer if md else "field",
                sentence=render_sentence(md, value) if md else None,
                benchmark=BenchmarkVerdict(**verdict) if verdict else None,
            )
        )

    blocked = engine.blocked_reasons(values)

    # ---- M3-3 / M3-6 ----
    stock_variance = _stock_variance(session, shop.id, start, end, values.get("stock_variance_rate"))
    observations["stock_variance"] = stock_variance
    input_alerts = _input_alerts(session, shop.id, values, end)
    observations["input_alerts"] = input_alerts

    # M4-9 异常清单：按金额降序，老板先看钱最多的那个
    anomalies = build_anomalies(
        values,
        engine.metric_defs,
        engine.field_defs,
        shop.category,
        tier,
        stock_sentence=stock_variance.get("sentence"),
        stock_amount=values.get("stock_variance_amount"),
    )
    observations["anomalies"] = anomalies

    # ---- M5-1 / M5-0：应付账龄与量价差异 ----
    # 放进 observations（**不进三行结论**）：结论严格三行是门禁，
    # 而"欠谁钱、逾期几天"是要单独看的一块，塞进结论会挤掉更重要的那句话。
    observations["payables"] = _payables_summary(session, shop.id, end)
    observations["bom_variance"] = _bom_variance_summary(session, shop.id, start, end)

    conclusion = build_conclusion(
        values=values,
        metric_defs=engine.metric_defs,
        field_defs=engine.field_defs,
        category=shop.category,
        days_span=days_span,
        days_with_data=days_with_data,
        observations=observations,
        blocked=blocked,
        trend=trend,
        stock_variance=stock_variance,
        input_alerts=input_alerts,
    )

    return ReportOut(
        shop=ShopOut.model_validate(shop),
        period=PeriodOut(
            start=start.isoformat(),
            end=end.isoformat(),
            days_with_data=days_with_data,
            days_span=days_span,
            month=month_first_day(end).strftime("%Y-%m"),
            tier=tier,
        ),
        metrics=metrics,
        conclusion=conclusion,
        observations=observations,
        blocked=blocked,
        trend=trend,
        anomalies=anomalies,
    )


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@router.get("", response_model=ReportOut, summary="区间报告（指标 + 三行结论 + 环比）")
def get_report(
    shop_id: int = Query(...),
    start: date = Query(..., description="起始日 YYYY-MM-DD"),
    end: date = Query(..., description="结束日 YYYY-MM-DD"),
    session: Session = Depends(get_db),
) -> ReportOut:
    return assemble_report(session, shop_id, start, end)


@router.get("/weekly", response_model=ReportOut, summary="周汇总（默认锚点往前 7 天）")
def get_weekly(
    shop_id: int = Query(...),
    anchor: date = Query(default=None, description="汇总截止日，默认今天"),
    days: int = Query(default=7, ge=1, le=90),
    session: Session = Depends(get_db),
) -> ReportOut:
    anchor = anchor or date.today()
    return assemble_report(session, shop_id, anchor - timedelta(days=days - 1), anchor)


# --------------------------------------------------------------------------- #
# M4-3 · 分类成本结构（"你的成本是被哪一类推上去的"）
# --------------------------------------------------------------------------- #
def _month_shift(d: date, delta: int) -> date:
    """月份加减。只用年月做算术，避免 datetime 的边界坑。"""
    total = (d.year * 12 + d.month - 1) + delta
    return date(total // 12, total % 12 + 1, 1)


def category_cost(
    session: Session,
    shop_id: int,
    start: date,
    end: date,
    *,
    level: int = 1,
    compare_previous: bool = True,
) -> dict[str, Any]:
    """按分类聚合采购金额，并和上一等长区间比。

    这一张表回答的是：**我的钱花在哪些类上、哪一类在涨**。
    分类的价值不在好看 —— 在能解释"食材成本率为什么从 30% 涨到 41%"。

    ⚠️ 口径：金额直接 Σ `purchases.amount`（原子量可加），
    **占比是 Σ分子 ÷ Σ分母重算**，不是"每天占比取平均"。
    """
    days_span = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days_span - 1)

    def _aggregate(s: date, e: date) -> tuple[dict[str, float], float]:
        rows = session.scalars(
            select(Purchase).where(
                Purchase.shop_id == shop_id,
                Purchase.purchase_date >= s,
                Purchase.purchase_date <= e,
            )
        ).all()
        buckets: dict[str, float] = {}
        total = 0.0
        for row in rows:
            code = row.category_code
            total += float(row.amount or 0.0)
            key = code.split("-")[0] if (code and level == 1) else (code or "未分类")
            buckets[key] = buckets.get(key, 0.0) + float(row.amount or 0.0)
        return buckets, total

    current, total = _aggregate(start, end)
    previous, prev_total = _aggregate(prev_start, prev_end) if compare_previous else ({}, 0.0)

    names = {c["code"]: c["name"] for c in CATEGORY_SEEDS}
    items: list[dict[str, Any]] = []
    for code, amount in current.items():
        prev_amount = previous.get(code)
        share = amount / total if total else 0.0
        prev_share = (prev_amount / prev_total) if (prev_amount is not None and prev_total) else None
        delta = (share - prev_share) if prev_share is not None else None
        direction = "平"
        if delta is not None:
            direction = "升" if delta > 0.005 else ("降" if delta < -0.005 else "平")
        items.append(
            {
                "code": code,
                "name": names.get(code, code if code != "未分类" else "未分类"),
                "level": level,
                "amount": round(amount, 2),
                "share": round(share, 6),
                "previous_amount": round(prev_amount, 2) if prev_amount is not None else None,
                "previous_share": round(prev_share, 6) if prev_share is not None else None,
                "share_delta": round(delta, 6) if delta is not None else None,
                "direction": direction,
                "is_main_driver": False,
            }
        )
    items.sort(key=lambda x: x["amount"], reverse=True)

    # 主因判定：占比涨幅最大的那一类（且涨幅有实质意义、金额也排得上）
    drivers = [i for i in items if i["share_delta"] is not None and i["share_delta"] >= 0.01]
    main_driver = max(drivers, key=lambda x: x["share_delta"]) if drivers else None
    if main_driver is not None:
        main_driver["is_main_driver"] = True

    conclusion = None
    if main_driver is not None and total:
        conclusion = (
            f"{main_driver['name']}占比从 {main_driver['previous_share']:.0%} "
            f"涨到 {main_driver['share']:.0%}，是这段时间食材成本变化的主因"
            f"（这一类花了 {main_driver['amount']:,.0f} 元，占 {main_driver['share']:.0%}）。"
        )
    elif total:
        top = items[0]
        conclusion = f"这段时间进货 {total:,.0f} 元，最大的一类是{top['name']}（{top['share']:.0%}）。"

    return {
        "total": round(total, 2),
        "coverage": round(
            sum(v for k, v in current.items() if k != "未分类") / total if total else 0.0, 6
        ),
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "previous_period": {"start": prev_start.isoformat(), "end": prev_end.isoformat()}
        if compare_previous
        else None,
        "items": items,
        "conclusion": conclusion,
    }


@router.get("/category-cost", summary="分类成本结构（M4-3）")
def get_category_cost(
    shop_id: int = Query(...),
    start: date = Query(...),
    end: date = Query(...),
    level: int = Query(default=1, ge=1, le=2, description="1=按大类，2=按中类"),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    require_shop(session, shop_id)
    if start > end:
        start, end = end, start
    return category_cost(session, shop_id, start, end, level=level)


# --------------------------------------------------------------------------- #
# M4-9 · 近 N 个月趋势（营业额 vs 保本线两条线）
# --------------------------------------------------------------------------- #
@router.get("/monthly-series", response_model=MonthlySeriesOut, summary="近 N 个月趋势（M4-9）")
def get_monthly_series(
    shop_id: int = Query(...),
    months: int = Query(default=12, ge=1, le=36),
    anchor: date = Query(default=None, description="以哪个月为最后一个点，默认今天所在月"),
    session: Session = Depends(get_db),
) -> MonthlySeriesOut:
    require_shop(session, shop_id)
    anchor = anchor or date.today()
    points: list[dict[str, Any]] = []
    for offset in range(months - 1, -1, -1):
        m_start = _month_shift(anchor, -offset)
        m_end = _month_shift(m_start, 1) - timedelta(days=1)
        # persist=False：趋势只是拿来看的，不该往 metric_value 缓存里塞 12 个月的快照
        values = MetricsEngine(session).compute(shop_id, m_start, m_end, persist=False)
        points.append(
            {
                "month": m_start.strftime("%Y-%m"),
                "revenue": values.get("revenue_m"),
                "breakeven_daily": values.get("breakeven_daily"),
                "daily_avg_revenue": (
                    round(values["revenue_m"] / 30, 2) if values.get("revenue_m") else None
                ),
            }
        )
    session.commit()
    return MonthlySeriesOut(points=points)
