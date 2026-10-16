"""审批与损耗原因代码化（M5-2，M6 修订阈值关系）。

## 为什么原因必须代码化

任务书原话：**"不代码化就不能聚合分析，等于白记。"**
自由文本里"坏了""倒了""扔了""变质"没法聚合，报表明明有数据却回答不了
"我的损耗到底主要发生在哪一环"。所以原因一律走 T01–T08 代码。

## 阈值规则（M5 §5 定，M6 §0 裁定 2 修订）

超过 **±100 元** 的盘盈盘亏 / 报废，**没有审批人就拒绝提交**；
比例（> 5%）只做**兜底**判据，与金额条件取「**且**」。

> **M6 为什么从「或」改成「且」**：餐饮业损耗率 5%–10% 是常态区间，
> 按"或"判定，正常经营的店几乎每笔报废都要找审批人 ——
> 最后一定是全部不填/乱填，审批流名存实亡。
> 评审裁定：**金额当主闸门，比例兜底**（仍然抓"小额但比例离谱"）。
> 双向断言见 `tests/test_m5.py::test_approval_threshold_requires_both_amount_and_rate`。
"""

from __future__ import annotations

from typing import Any

#: 损耗 / 报废 / 盘亏原因代码（v0.2 §3.2 口径 + 任务书 §5 列表）
LOSS_REASONS: dict[str, str] = {
    "T01": "运输破损",
    "T02": "存储变质",
    "T03": "分拣损耗",
    "T04": "盗窃",
    "T05": "过期",
    "T06": "制作失败",
    "T07": "退菜",
    "T08": "盘亏",
}

#: 审批阈值
#:
#: ⚠️ **M6 按 `docs/06-M6` §0 裁定 2 改了判定关系：从「或」改成「且」**
#:
#!    金额 > 100 元  **且**  比例 > 5%   → 才要审批人
#:
#: 原来是「或」，结果 5% 的比例闸门在餐饮业是常态区间（行业损耗率普遍 5%–10%）——
#! 正常经营的店几乎每笔报废都要签字，最后必然是"全部不填/乱填"，审批流名存实亡。
#: 评审裁定：**金额当主闸门，比例只做兜底**（抓"小额但比例离谱"的情况）。
#:
#: 门禁 3 的双向断言（`docs/06-M6` §6）：
#!   * 28 元 / 9%   → 金额没过线 → **不需审批**
#!   * 500 元 / 6%  → 金额过线且比例过线 → **需要审批**
APPROVAL_AMOUNT_THRESHOLD = 100.0    # 元
APPROVAL_RATE_THRESHOLD = 0.05       # 5%


class ApprovalRequiredError(ValueError):
    """超过阈值但没有审批人。"""


def normalize_reason(code: str | None) -> str | None:
    """校验原因代码。给了代码但不在册 → 直接拒绝（不许悄悄存成自由文本）。"""
    if code is None or not str(code).strip():
        return None
    cleaned = str(code).strip().upper()
    if cleaned not in LOSS_REASONS:
        valid = "、".join(f"{k}（{v}）" for k, v in LOSS_REASONS.items())
        raise ValueError(f"损耗原因必须是代码之一：{valid}")
    return cleaned


def needs_approval(amount: float | None, base: float | None) -> tuple[bool, str]:
    """要不要审批。返回（是否需要，原因说明）。

    `amount` = 差异/报废金额（元）；`base` = 参照基数（如账面金额）。

    **判定关系是「且」**（M6 裁定 2）：金额和比例**都**超线才要签字。
    金额是主闸门，比例只兜底 —— 单看比例会把行业常态损耗（5%–10%）当异常。
    """
    amt = abs(float(amount or 0.0))
    over_amount = amt > APPROVAL_AMOUNT_THRESHOLD
    over_rate = False
    rate_text = ""
    if base:
        rate = amt / abs(float(base))
        over_rate = rate > APPROVAL_RATE_THRESHOLD
        rate_text = f"，比例 {rate:.1%}（阈值 {APPROVAL_RATE_THRESHOLD:.0%}）"

    if over_amount and over_rate:
        return True, (
            f"金额 {amt:,.0f} 元超过 {APPROVAL_AMOUNT_THRESHOLD:,.0f} 元"
            f"{rate_text}"
        )
    return False, ""


def require_approval(
    *, amount: float | None, base: float | None, approver: str | None, what: str = "这笔单"
) -> dict[str, Any]:
    """超阈值没人签字 → 抛异常（路由翻译成 400）。

    返回一段说明，方便调用方把"为什么放行/为什么拦"写进日志或响应里。
    """
    need, why = needs_approval(amount, base)
    if need and not (approver or "").strip():
        raise ApprovalRequiredError(
            f"{what}触发了审批阈值（{why}），必须填审批人才能提交 —— "
            f"没签字的账最后没人认。"
        )
    return {
        "needs_approval": need,
        "reason": why,
        "approver": (approver or None) if need else None,
    }


def reason_breakdown(rows: list[Any], attr: str = "reason_code") -> list[dict[str, Any]]:
    """把损耗按原因代码聚合（这就是"代码化"换来的东西）。"""
    buckets: dict[str, dict[str, float]] = {}
    for row in rows:
        code = getattr(row, attr, None) or "未分类"
        item = buckets.setdefault(code, {"amount": 0.0, "count": 0.0})
        item["amount"] += float(getattr(row, "amount", 0.0) or 0.0)
        item["count"] += 1
    out = [
        {
            "code": code,
            "name": LOSS_REASONS.get(code, "未分类"),
            "amount": round(v["amount"], 2),
            "count": int(v["count"]),
        }
        for code, v in buckets.items()
    ]
    out.sort(key=lambda x: x["amount"], reverse=True)
    total = sum(i["amount"] for i in out) or 1.0
    for item in out:
        item["share"] = round(item["amount"] / total, 6)
    return out
