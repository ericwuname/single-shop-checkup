#!/usr/bin/env python
"""生成「回话速查卡」（M6-3，docs/06 §4.2）。

按 `server/benchmarks.py` 里的区间，生成一张
「数字区间 → 一句话」对照表。存手机或打印，收数时不用现想话术。

用法：
    python scripts/gen_quickref_card.py                 # 打印到屏幕
    python scripts/gen_quickref_card.py --out docs/回话速查卡.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 分类 → 一句话模板。{low}/{high} 是区间端点，由 benchmarks 填。
#:
#: 为什么话术要分三档：同一个数字，落在正常区间、逼近预警、已经越界，
#: 该说的话完全不同。**说错了比不说更糟** —— 老板会以为你在下结论。
TEMPLATES = {
    "labor_cost_rate_full": (
        "人工占营业额 {value}",
        "正常区间是 {low}–{high}。"
        "低了要想是不是有人没排够；高了先看是不是这个月生意淡但人没减。",
        "已经超过 {high} 了。先别动人数 —— 看是营业额掉下来还是加班变多，两种情况对策相反。",
    ),
    "food_cost_rate": (
        "食材占营业额 {value}",
        "正常区间是 {low}–{high}。这个数越低越可能是采购漏记，不是越省。",
        "超过 {high} 了。先分清是买贵了还是用浪费了 —— 前者找供应商，后者找厨房。",
    ),
    "efficiency": (
        "每小时人工产出 {value} 元",
        "正常区间是 {low}–{high}。低了通常是营业额掉了，不是人变懒。",
        "低于 {low} 了。这个月是不是客人少了？人没减的话，先看能不能把班次并一下。",
    ),
    "avg_ticket": (
        "客单价 {value} 元",
        "正常区间是 {low}–{high}。想提客单，先看是不是有人在点单时被推荐得不够。",
        "低于 {low} 了。先别急着涨价 —— 涨价会把客人推走，先看套餐搭得对不对。",
    ),
    "breakeven_daily": (
        "每天要做到 {value} 元才不亏",
        "对比上周的保本线看趋势。涨太快说明固定成本在爬。",
        "保本线比上周又高了。先把新增的固定开销列出来，看哪笔是必须的。",
    ),
    "stock_variance_rate": (
        "账实差了 {value}",
        "差异越小越好。有了成本卡之后，这个数就是跑冒滴漏。",
        "差异偏大。先看是不是有货没录领用，再查是不是真丢了。",
    ),
    "explained_loss_rate": (
        "损耗里有 {value} 说得清原因",
        "越高越好。剩下说不清的那部分，才是真正要查的。",
        "说不清的比例偏高。下次报损时把原因代码选上，月底才好归类。",
    ),
}


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float) and 0 < v < 1:
        return f"{v:.0%}"
    if isinstance(v, (int, float)):
        return f"{v:,.0f}"
    return str(v)


def build_card() -> str:
    from server.benchmarks import BENCHMARKS

    lines: list[str] = []
    lines.append("# 回话速查卡")
    lines.append("")
    lines.append("> 收数时不用现想话术：看数字落在哪一档，念对应那句就行。")
    lines.append("> **只给数字 + 区间 + 一个动作，不下结论**（项目铁律）。")
    lines.append("")
    lines.append("")

    # benchmarks 的结构是 {分类: {指标: {...}}}，取 catering 那一层
    catering = BENCHMARKS.get("catering", {})
    for code, (name_tpl, ok_line, warn_line) in TEMPLATES.items():
        bench = catering.get(code)
        if bench is None:
            continue
        low = _fmt(bench.get("low"))
        high = _fmt(bench.get("high"))
        display = bench.get("display") or (f"{low}–{high}" if bench.get("low") is not None else None)
        title = bench.get("label", code) + (f"（正常区间 {display}）" if display else "")
        lines.append(f"## {title}")
        lines.append("")
        ok_text = bench.get("advice_ok") or ok_line.format(low=low, high=high)
        action_ok = bench.get("action_ok")
        if action_ok:
            lines.append(f"- ✅ **正常**：{ok_text} → {action_ok}")
        else:
            lines.append(f"- ✅ **正常**：{ok_text}")
        for key, label in (("advice_high", "⚠️ **偏高**"), ("advice_low", "⚠️ **偏低**")):
            text = bench.get(key)
            if not text:
                continue
            action = bench.get(key.replace("advice", "action"))
            lines.append(f"- {label}：{text}" + (f" → {action}" if action else ""))
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 三个铁的回应原则")
    lines.append("")
    lines.append("1. **先念数字，再说区间** —— 老板只信自己店里的数，不信行业平均")
    lines.append("2. **只给一个动作** —— 给三个动作等于没给")
    lines.append("3. **不替老板下结论** —— 不说\"你应该裁员/关店\"，只说\"这里可以查一下\"")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成回话速查卡")
    parser.add_argument("--out", help="输出文件（默认打印到屏幕）")
    args = parser.parse_args()

    card = build_card()
    if args.out:
        Path(args.out).write_text(card, encoding="utf-8")
        print(f"已写出：{args.out}")
    else:
        print(card)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
