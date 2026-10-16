"""业态基准区间常量 + 大白话话术（施工单 3.2 / server/benchmarks.py）。

为什么单独一个文件：基准值和话术是**运营口径**，会随谈的店、随行业数据变，
必须和代码逻辑分开 —— 改数改话术不改逻辑。

每条话术的硬约束（施工单 1.4）：**绝不出现财务术语**。
黑名单在 tests/test_daily.py::FORBIDDEN_TERMS，由单测守门。

三类文案分工，对应施工单 1.4 的"三行"：
    advice_*  → 第二行（判断）
    action_*  → 第三行（一个动作）

------------------------------------------------------------------ M2 增补 1：source 字段
`docs/01` §4 缺陷 1 的教训是"区间凭感觉填、且改了区间忘改话术"。
所以每个区间现在必须带 `source`，写清这个数从哪来。**没有查证依据的，写"待查证"，不许编。**

------------------------------------------------------------------ M2 增补 2：话术分档（docs/02 M2-2）
`action_*` / `advice_*` 仍然只写"日/周档"的口吻（人话模板），
月/季档与年度档由 `action_for_tier` / `verdict_for_tier` **机械改写**得到 ——
不复制三份文案，避免将来改一句只改到了一档。

分档边界（docs/02 M2-2）：≤7 天 = 日/周；8–90 天 = 月/季；>90 天 = 年度。
"""

from __future__ import annotations

from typing import Any

# =========================================================================== #
# 区间长度分档
# =========================================================================== #
TIER_DAY = "day"
TIER_MONTH = "month"
TIER_YEAR = "year"

TIER_DAY_MAX = 7      # ≤7 天 → 日/周档
TIER_YEAR_MIN = 90    # >90 天 → 年度档；8–90 天 → 月/季档

TIERS: dict[str, dict[str, str]] = {
    TIER_DAY: {
        "label": "日/周",
        "verdict_prefix": "",
        "action_prefix": "",
        "period_word": "这几天",
        "default_verdict": "这几个数看着都在正常区间。",
        "default_action": "明天照常记三个数就行，攒够 7 天再看一次汇总。",
    },
    TIER_MONTH: {
        "label": "月/季",
        "verdict_prefix": "这段时间",
        "action_prefix": "这个月先做一件事：",
        "period_word": "这段时间",
        "default_verdict": "这段时间这几个数看着都在正常区间。",
        "default_action": "这个月照常记三个数就行，月底再看一次汇总。",
    },
    TIER_YEAR: {
        "label": "年度",
        "verdict_prefix": "这一年",
        "action_prefix": "明年最先要动的是：",
        "period_word": "这一年",
        "default_verdict": "这一年这几个数看着都在正常区间。",
        "default_action": "明年照常记三个数就行，一年做一次这样的体检。",
    },
}


def tier_for(days_span: int) -> str:
    """区间长度 → 话术档位。"""
    if days_span <= TIER_DAY_MAX:
        return TIER_DAY
    if days_span <= TIER_YEAR_MIN:
        return TIER_MONTH
    return TIER_YEAR


def tier_label(tier: str) -> str:
    return TIERS.get(tier, TIERS[TIER_DAY])["label"]


def action_for_tier(action_text: str | None, tier: str) -> str:
    """把"日/周档"的动作改写成本档口吻。

    规则（机械、可测）：
      0. **日/周档直接原样返回** —— 人话模板就是给日档写的，一个字符都不动
         （docs/02 §3 M2-2 要求"7 天档仍含明天，不许误伤"）；
      1. 非日档：去掉句首的「明天」/「今天」；
      2. 非日档：去掉「昨天的」这类日尺度指代；
      3. 套上本档前缀；
      4. 兜底：非日档不允许残留「明天」（docs/02 的硬判据）。
    """
    if not action_text:
        return action_text or ""

    if tier == TIER_DAY:
        return action_text

    body = action_text.strip()
    for lead in ("明天", "今天"):
        if body.startswith(lead):
            body = body[len(lead):]
            break

    body = body.replace("昨天的", "").replace("昨天", "")
    body = TIERS.get(tier, TIERS[TIER_DAY])["action_prefix"] + body
    return body.replace("明天", "")


def verdict_for_tier(advice_text: str | None, tier: str) -> str:
    """给判断行套上本档的时间口吻。"""
    if not advice_text:
        return ""
    prefix = TIERS.get(tier, TIERS[TIER_DAY])["verdict_prefix"]
    if not prefix or advice_text.startswith(prefix):
        return advice_text
    return prefix + advice_text


def period_word(tier: str) -> str:
    return TIERS.get(tier, TIERS[TIER_DAY])["period_word"]


# =========================================================================== #
# 餐饮（M1 唯一业态；社区快餐/小吃口径）
# =========================================================================== #
CATERING: dict[str, dict[str, Any]] = {
    "food_cost_rate": {
        "label": "食材成本率",
        # M2-0（docs/02 §2）：18%–22% → 28%–38%。旧的区间会让**所有**快餐店都报"偏高"，
        # 第三次之后老板就不信预警了，等于把产品的可信度一次烧掉。
        "low": 0.28,
        "high": 0.38,
        "display": "28%–38%",
        "source": (
            "塔斯汀 35%–38% / 十尾鲜 29.9% / 行业参考 30%–40%（被评成本控制优异）"
            "/ 社区小馆 35%–45%；本区间取社区快餐口径 28%–38%。"
            "2026-10 由评审窗口查证，见 docs/01 §4 缺陷 1。"
        ),
        "higher_better": 0,
        "advice_high": "食材占比偏高，快餐一般 28%–38%。",
        "advice_low": "食材占比偏低，可能是采购漏记了，也可能这几天在消化存货。",
        "advice_ok": "食材占比在正常区间，保持。",
        "action_high": "明天买菜前先看一眼昨天的采购单，把囤货和当天要用的分开记。",
        "action_low": "明天把采购的票据留着，晚上对一遍是不是有漏记的。",
        "action_ok": "明天照常记三个数就行。",
    },
    "labor_cost_rate": {
        "label": "人力成本率",
        # M3-1（docs/03 §2）：15%–25% → 15%–20%，预警 >22%。
        # 查证：快餐/简餐 15%-20%、火锅 18%-22%、正餐 20%-25%、自助快餐 15%-22%；
        # 健康区间 17%-22%，>25% 判危险；三利快餐实测 14.3%（行业基准 12%-18%）。
        "low": 0.15,
        "high": 0.20,
        "display": "15%–20%",
        "source": (
            "快餐/简餐 15%–20%、火锅 18%–22%、正餐 20%–25%、自助快餐 15%–22%；"
            "健康区间 17%–22%，>25% 判危险；三利快餐实测 14.3%（行业基准 12%–18%）。"
            "本区间取快餐口径 15%–20%，预警线 22%。"
            "2026-10 由评审窗口查证，见 docs/03 §0「7-5 补查证」。"
        ),
        "higher_better": 0,
        "advice_high": "人工占比偏高，快餐一般 15%–20%。",
        "advice_low": "人工占比偏低，人手可能不太够。",
        "advice_ok": "人工占比正常。",
        "action_high": "明天把最闲那一小时的排班往后压一压，试试人流跟不跟得上。",
        "action_low": "看看高峰期出餐速度，别把老板自己顶班的时间漏算了。",
        "action_ok": "明天照常记三个数就行。",
    },
    "avg_ticket": {
        "label": "客单价",
        "low": 15.0,
        "high": 45.0,
        "display": "15–45 元",
        # M3-1 要求：保留区间，但 source 必须写明"跨度为什么收不窄"
        "source": (
            "保留 15–45 元，**不收窄**。查证：自助快餐主流 12–25 元、三利快餐 18 元、"
            "塔斯汀 25–30 元、社区牛肉面 16 元、荔林食店 46 元 —— **业态内差异 12–46 元**，"
            "跨度大到无法用一个区间覆盖。M3 应按细分业态拆分（已列为观察项）。"
            "2026-10 由评审窗口查证，见 docs/03 §0「7-5 补查证」。"
        ),
        "higher_better": 1,
        "advice_high": "客单价偏高，要注意是不是靠几个大单撑起来的。",
        "advice_low": "客单价偏低。",
        "advice_ok": "客单价在正常区间。",
        "action_high": "明天把大单和散客分开看一眼，散客还来不来。",
        "action_low": "明天试试把主食和饮料搭配着推荐，看客单价动不动。",
        "action_ok": "明天照常记三个数就行。",
    },
    "efficiency": {
        "label": "人效",
        # M3-1（docs/03 §2）：80–300 → 120–180 元/工时。
        # 旧区间下限 80 是**正餐**标准，对快餐会漏报。
        "low": 120.0,
        "high": 180.0,
        "display": "120–180 元/小时",
        "source": (
            "快餐/简餐 120–180 元/时、正餐 80–130、茶饮 150–200；行业目标值 ≥160 元/小时。"
            "本区间取快餐口径 120–180。旧区间下限 80 是正餐标准，对快餐会漏报。"
            "2026-10 由评审窗口查证，见 docs/03 §0「7-5 补查证」。"
        ),
        "higher_better": 1,
        "advice_high": "人效偏高，人可能用得太狠了。",
        "advice_low": "人效偏低，闲着的人可能排多了。",
        "advice_ok": "人效在正常区间。",
        "action_high": "明天留意忙时段有没有掉单、出餐有没有排队。",
        "action_low": "明天把闲时段的排班减一个人试试。",
        "action_ok": "明天照常记三个数就行。",
    },
    # 保本营业额没有"同行区间"概念：只陈述、不比较高下（M2-4 改为与自己的上期对比）
    "breakeven_daily": {
        "label": "保本营业额",
        "low": None,
        "high": None,
        "display": None,
        "source": "无同行区间：保本额取决于本店房租与人工，没有行业标准。M2-4 改为与上一期自比对。",
        "higher_better": None,
        "advice_high": "",
        "advice_low": "",
        "advice_ok": "这是你的底线：每天做到这个数才不亏。",
        "action_high": "",
        "action_low": "",
        "action_ok": "盯住这个数，哪几天没做到，就把那天单独翻出来看。",
    },
}

BENCHMARKS: dict[str, dict[str, dict[str, Any]]] = {"catering": CATERING}

DEFAULT_CATEGORY = "catering"

#: 判断行的优先级（越靠前越先拿出来说）
#:
#: M4-0（docs/04 §1 裁定 6-3）：**判断走全口径，不走排班工钱**。
#: `labor_cost_rate` 的口径是"排班工钱"，在只走"全职月薪"约定的店里会掉到个位数百分比，
#: 拿 15%–20% 的区间去比会报"人工偏低、人手不够"——那是误导。所以它已被置为不给判断
#: （benchmark_key=None），判断改由 `labor_cost_rate_full` 承担。
VERDICT_PRIORITY: tuple[str, ...] = (
    "food_cost_rate",
    "labor_cost_rate_full",
    "efficiency",
    "avg_ticket",
)


def benchmark_for(category: str | None, key: str | None) -> dict[str, Any] | None:
    if not key:
        return None
    return BENCHMARKS.get(category or DEFAULT_CATEGORY, {}).get(key)


#: 区间条的轴余量：轴 = [low − 0.5×宽, high + 0.5×宽]。
#: 为什么要留余量：只有让轴比"正常区间"更宽，越界的指针才有地方可走 ——
#: 否则超出一点就贴边，老板看不出"超了多少"。
BAR_MARGIN = 0.5


def _position(value: float, low: float, high: float) -> float:
    """把"你的位置"映射到区间条上的 0–1 坐标（M4-8 的指针）。

    轴 = [low − 0.5×宽, high + 0.5×宽]，于是：
      * 值 = low  → **0.25**（正常区间的左沿）
      * 值 = high → **0.75**（正常区间的右沿）
      * 值 = 轴左端 → 0，值 = 轴右端 → 1
    正常区内指针落在 0.25–0.75（绿区），越界才会走出去 —— 一眼能看出超了多少。
    """
    if high <= low:
        return 0.5
    width = high - low
    axis_min = low - BAR_MARGIN * width
    axis_max = high + BAR_MARGIN * width
    return round(min(1.0, max(0.0, (value - axis_min) / (axis_max - axis_min))), 6)


def compare(
    category: str | None,
    key: str | None,
    value: float | None,
    tier: str = TIER_DAY,
) -> dict[str, Any] | None:
    """返回 {status, text, action, range_display, source, low, high, position}。

    无基准或无值返回 None。
    status ∈ ok / high / low / none（none = 该指标没有同行区间，只陈述）
    text / action 已按 `tier` 改写口吻。
    low/high/position 是给 M4-8 画区间条用的（含指针位置）。
    """
    bench = benchmark_for(category, key)
    if bench is None or value is None:
        return None

    low, high = bench.get("low"), bench.get("high")
    display = bench.get("display")

    if low is None or high is None:
        # 无同行区间（如保本额）：只陈述，且**不画条** —— low/high 留 None
        return {
            "status": "none",
            "text": verdict_for_tier(bench.get("advice_ok", ""), tier),
            "action": action_for_tier(bench.get("action_ok", ""), tier),
            "range_display": display,
            "source": bench.get("source"),
            "low": None,
            "high": None,
            "position": None,
        }

    if value > high:
        status = "high"
    elif value < low:
        status = "low"
    else:
        status = "ok"

    return {
        "status": status,
        "text": verdict_for_tier(bench.get(f"advice_{status}", ""), tier),
        "action": action_for_tier(bench.get(f"action_{status}", ""), tier),
        "range_display": display,
        "source": bench.get("source"),
        "low": float(low),
        "high": float(high),
        "position": _position(float(value), float(low), float(high)),
    }
