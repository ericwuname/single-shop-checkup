"""MetricsEngine —— 指标注册表解释器（施工单 3.5，项目最关键的一块）。

一句话：**指标不算在代码里，算在表里。** 这个模块只做四件事——
读 metric_def → 解析 formula → 按依赖顺序求值 → 写回缓存。

------------------------------------------------------------------ 四条守卫（施工单 3.5）
1. 分母为 0 或缺失        → 返回 None。不报错、**不算成 0**。
2. 派生（比率类）指标做分母 → 抛 DerivedMetricAsDivisorError。
3. 公式引用不存在的字段/指标 → 抛 UnknownFieldError。
4. 跨期聚合               → 先 Σ 原子，再算比率。**禁止对比率取平均**。

------------------------------------------------------------------ 与施工单的一处必要澄清
施工单把守卫 2 写成「公式引用了派生指标做分母 → 抛异常」。若按字面实现，
施工单 §3.4 自己列出的 `efficiency = revenue / hours` 会被判死——
因为 hours 也是派生出来的（来自 shifts 表）。

所以引擎必须区分「可加的派生量」和「不可加的派生比率」：
  - hours / labor_cost / labor_m  → aggregation='additive'（跨期可加，做分母安全）
  - food_cost_rate / labor_cost_rate 等 → aggregation='ratio'（跨期不可加，
    拿来当分母做跨期计算一定错，必须拦）
守卫 2 的真实落点就是后者。metric_def 为此新增 aggregation 列（原列一个没删）。
见 docs/M1-交付报告.md §「与施工单的偏差」。

------------------------------------------------------------------ 其余工程决定
* 公式求值用 ``ast`` 白名单，不用 eval。只放行 常量 / 变量 / + - * / / 一元负号 /
  四个安全函数（min max abs round）。属性访问、下标、lambda、推导式、幂运算全部拒绝。
* 原子值的两条来源：
    a) field_def 定义的字段 —— 从 fact_daily 聚合（日口径 SUM，月口径取当月值）
    b) 引擎注入项（hours / labor_cost / labor_m）—— 从 shifts 现算
* 月口径锚点：查询区间的 **end 所在月**。因为 breakeven_daily 的分母是"这个月的固定开销"，
  跨月区间只能取一个代表月，取区间末尾最符合"我现在该做多少"的直觉。
"""

from __future__ import annotations

import ast
import calendar as _calendar
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import FactDaily, FieldDef, MetricDef, MetricValue, Shift

# 引擎内建注入的原子值：不由 field_def 定义，由事实表现算
#
# M3 扩充说明：这里新增的是**事实来源的聚合**（月口径营业额、盘点与报废的金额），
# 不是指标算法 —— 指标的算法依然只写在 metric_def.formula 里。
# 判定边界：只要"这个量能被 SUM 出来"就属于事实层；一旦"它等于别的量之比"就属于指标层。
INJECTED_ATOMIC: tuple[str, ...] = (
    # M1：从 shifts 现算
    "hours",
    "labor_cost",
    "labor_m",
    # M3-0：锚点月的营业额（供利润类指标用）+ 月化营业额（供跨口径比率用）
    "revenue_m",
    "purchase_m",
    "revenue_m_avg",
    # M3-3：从 stocktakes / scraps 现算
    "stock_book_amount",
    "stock_actual_amount",
    "scrap_amount",
    # M6（docs/06 §0 裁定 7）：可解释损耗率的分子（已登记且通过审核的损耗）
    "explained_loss_m",
)

# fact_daily 上有独立列的字段（其余字段走 extra_json 溢出通道）
TYPED_FIELD_COLUMNS: tuple[str, ...] = (
    "revenue",
    "purchase",
    "orders",
    "rent_m",
    "utility_m",
    "gross_margin",
)


# =========================================================================== #
# 异常
# =========================================================================== #
class MetricsError(Exception):
    """指标引擎异常基类。"""


class UnknownFieldError(MetricsError):
    """公式引用了 field_def / metric_def 里都不存在的东西。"""


class RatioAsInputError(MetricsError):
    """把 aggregation="ratio" 的量当成公式输入（M2-6 组合层输入约束）。

    比率是「算出来」的值，跨期既不能加也不能减。两个比率相加（如
    food_cost_rate + labor_cost_rate）在数学上毫无意义，但结果看着像个数，
    所以必须由引擎拒绝，不能靠人自觉。
    """


class DerivedMetricAsDivisorError(RatioAsInputError):
    """特例：比率出现在**分母**位置。

    继承 RatioAsInputError —— 做分母本来就是"拿比率当输入"的一种，
    只是诊断信息更具体，而且历史代码/测试按这个更窄的名字捕获它。
    """


class FormulaSyntaxError(MetricsError):
    """公式语法不合法，或用了白名单外的语法（属性访问/下标/函数调用等）。"""


class CyclicDependencyError(MetricsError):
    """指标互相依赖成环，无法拓扑排序。"""


# =========================================================================== #
# 公式解析器
# =========================================================================== #
_ALLOWED_BINOPS: tuple[type, ...] = (ast.Add, ast.Sub, ast.Mult, ast.Div)
_ALLOWED_UNARYOPS: tuple[type, ...] = (ast.USub, ast.UAdd)
_ALLOWED_FUNCS: dict[str, Any] = {"min": min, "max": max, "abs": abs, "round": round}


class FormulaParser:
    """极小的安全表达式求值器。只认公式需要的语法，其余一律拒绝。"""

    @staticmethod
    def parse(expr: str) -> ast.Expression:
        if expr is None:
            raise FormulaSyntaxError("公式为空")
        text = expr.strip()
        if not text:
            raise FormulaSyntaxError("公式为空")
        try:
            tree = ast.parse(text, mode="eval")
        except SyntaxError as exc:  # pragma: no cover - 依赖 Python 语法解析
            raise FormulaSyntaxError(f"公式语法错误：{text} —— {exc.msg}") from exc
        FormulaParser._check(tree)
        return tree

    # ---- 静态检查 -------------------------------------------------------- #
    @staticmethod
    def _check(node: ast.AST) -> None:
        if isinstance(node, ast.Expression):
            FormulaParser._check(node.body)
            return
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, _ALLOWED_BINOPS):
                raise FormulaSyntaxError(f"不支持的运算符：{type(node.op).__name__}")
            FormulaParser._check(node.left)
            FormulaParser._check(node.right)
            return
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, _ALLOWED_UNARYOPS):
                raise FormulaSyntaxError(f"不支持的一元运算符：{type(node.op).__name__}")
            FormulaParser._check(node.operand)
            return
        if isinstance(node, ast.Name):
            return
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                return
            raise FormulaSyntaxError(f"公式里只能是数字常量，收到：{node.value!r}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
                raise FormulaSyntaxError(f"不支持的函数调用：{ast.dump(node.func)}")
            if node.keywords:
                raise FormulaSyntaxError("函数不支持关键字参数")
            for arg in node.args:
                FormulaParser._check(arg)
            return
        raise FormulaSyntaxError(f"公式里不允许出现：{type(node).__name__}")

    # ---- 依赖提取 -------------------------------------------------------- #
    @staticmethod
    def _call_func_names(tree: ast.Expression) -> set[int]:
        """公式里被调用的函数名节点 id（用来把 `abs` / `min` 这些**函数名**排除出变量集）。

        曾经漏了这一步：`names()` 会把 `abs(stock_variance_amount)` 里的 `abs`
        当成一个变量收进依赖集，于是校验直接报"公式引用了不存在的字段：abs"。
        M1 的白名单支持 min/max/abs/round，但**没有任何种子指标用到**，
        所以这个坑一直没被踩到，直到 M3-3 第一次写出 `abs(...)`。
        """
        return {
            id(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

    @staticmethod
    def names(tree: ast.Expression) -> set[str]:
        """公式引用到的全部**变量**名（不含被调用的函数名）。"""
        func_names = FormulaParser._call_func_names(tree)
        return {
            n.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Name) and id(n) not in func_names
        }

    @staticmethod
    def divisor_names(tree: ast.Expression) -> set[str]:
        """只取「出现在分母位置」的变量名（同样排除函数名）。

        递归走所有 BinOp(Div)，收集其右子树的 Name：
        既覆盖 ``a / b``，也覆盖 ``a / (b * c)`` 与 ``min(x, a / b)``。
        """
        func_names = FormulaParser._call_func_names(tree)
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                found |= {
                    n.id
                    for n in ast.walk(node.right)
                    if isinstance(n, ast.Name) and id(n) not in func_names
                }
        return found

    # ---- 求值 ------------------------------------------------------------ #
    @staticmethod
    def evaluate(tree: ast.Expression, ctx: Mapping[str, float | None]) -> float | None:
        """求值。任何操作数为 None、或分母为 0 → 返回 None（守卫 1）。"""
        return FormulaParser._eval(tree.body, ctx)

    @staticmethod
    def _eval(node: ast.AST, ctx: Mapping[str, float | None]) -> float | None:
        if isinstance(node, ast.Constant):
            return float(node.value)

        if isinstance(node, ast.Name):
            value = ctx.get(node.id)
            if value is None:
                return None
            return float(value)

        if isinstance(node, ast.UnaryOp):
            operand = FormulaParser._eval(node.operand, ctx)
            if operand is None:
                return None
            return -operand if isinstance(node.op, ast.USub) else operand

        if isinstance(node, ast.BinOp):
            # 分母先算：分母缺失/为 0 时直接 None，不产生 0/0 或 1/0
            if isinstance(node.op, ast.Div):
                denominator = FormulaParser._eval(node.right, ctx)
                if denominator is None or denominator == 0:
                    return None
                numerator = FormulaParser._eval(node.left, ctx)
                if numerator is None:
                    return None
                return numerator / denominator
            left = FormulaParser._eval(node.left, ctx)
            right = FormulaParser._eval(node.right, ctx)
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            return left * right

        if isinstance(node, ast.Call):
            args = [FormulaParser._eval(a, ctx) for a in node.args]
            if any(a is None for a in args):
                return None
            fname = node.func.id  # _check 已保证是白名单内的 Name
            if fname == "round":
                # round(x, n) 的第二个参数必须是 int，而公式里所有常量都被读成 float
                if len(args) == 1:
                    return float(round(args[0]))
                return float(round(args[0], int(args[1])))
            return float(_ALLOWED_FUNCS[fname](*args))

        raise FormulaSyntaxError(f"求值时遇到未放行的节点：{type(node).__name__}")


# =========================================================================== #
# 小工具
# =========================================================================== #
def as_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def month_first_day(value: date | str) -> date:
    d = as_date(value)
    return d.replace(day=1)


def month_last_day(value: date | str) -> date:
    d = as_date(value)
    return d.replace(day=_calendar.monthrange(d.year, d.month)[1])


def parse_hhmm(value: str) -> tuple[int, int]:
    hh, mm = value.split(":")
    return int(hh), int(mm)


def shift_hours(start_time: str, end_time: str) -> float:
    """跨夜按 +24h 处理：22:00→06:00 = 8 小时（施工单 3.6）。"""
    sh, sm = parse_hhmm(start_time)
    eh, em = parse_hhmm(end_time)
    minutes = (eh * 60 + em) - (sh * 60 + sm)
    if minutes < 0:
        minutes += 24 * 60
    return round(minutes / 60.0, 4)


def _read_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# =========================================================================== #
# 引擎
# =========================================================================== #
class MetricsEngine:
    def __init__(self, session: Session):
        self.session = session
        self.field_defs: dict[str, FieldDef] = {}
        self.metric_defs: dict[str, MetricDef] = {}
        self._trees: dict[str, ast.Expression] = {}
        self._names: dict[str, set[str]] = {}
        self._divisors: dict[str, set[str]] = {}
        self.reload()

    # ---- 载入 ------------------------------------------------------------ #
    def reload(self) -> None:
        self.field_defs = {
            fd.code: fd
            for fd in self.session.scalars(select(FieldDef).where(FieldDef.enabled == 1)).all()
        }
        self.metric_defs = {
            md.code: md
            for md in self.session.scalars(select(MetricDef).where(MetricDef.enabled == 1)).all()
        }
        self._trees, self._names, self._divisors = {}, {}, {}
        for code, md in self.metric_defs.items():
            formula = (md.formula or "").strip()
            # 注意：这里**不再**按 layer 短路。
            # layer 表达"这个量的性质"（原子级可加 / 派生比率），
            # 有没有 formula 才表达"值怎么来的"。M2-1 的 labor_m_full 就是
            # layer=atomic + 有 formula（若干 additive 相加，仍是 additive）。
            if not formula:
                continue
            tree = FormulaParser.parse(formula)
            self._trees[code] = tree
            self._names[code] = FormulaParser.names(tree)
            self._divisors[code] = FormulaParser.divisor_names(tree)

    # ---- 校验（守卫 2 / 3 + M2-6 组合层输入约束） ------------------------- #
    @staticmethod
    def is_ratio_aggregated(md: MetricDef) -> bool:
        """判定依据是 `aggregation` 字段，**不是**"是否派生"（docs/02 M2-6）。

        为什么不能用 layer 判：`efficiency = revenue / hours` 里的 `hours` 也是
        派生出来的（从 shifts 现算），但它是 additive，做分母完全合法。
        按 layer 判会把施工单 §3.4 自己列的指标判死（M1 §5-1 已澄清过这一点）。
        """
        return (md.aggregation or "ratio") == "ratio"

    #: M1 时期的旧名字，保留以免外部引用断掉
    is_non_additive = is_ratio_aggregated

    def validate(self) -> None:
        field_codes = set(self.field_defs)
        metric_codes = set(self.metric_defs)

        for code, md in self.metric_defs.items():
            formula = (md.formula or "").strip()

            # --- 无公式 = 直接取数型：必须能从 field_def 或引擎注入拿到值 ---
            if not formula:
                if code not in field_codes and code not in INJECTED_ATOMIC:
                    raise UnknownFieldError(
                        f"量 {code!r} 既没有公式，也不在 field_def 中定义，"
                        f"也不是引擎注入项 {INJECTED_ATOMIC}"
                    )
                continue

            # --- 守卫 3：引用的名字必须存在 ---
            for name in sorted(self._names.get(code, set())):
                if name not in field_codes and name not in metric_codes:
                    raise UnknownFieldError(
                        f"指标 {code!r} 的公式引用了不存在的字段/指标：{name!r}"
                    )

            divisors = self._divisors.get(code, set())

            # --- 守卫 2（比率做分母，诊断更具体 → 先判）---
            for name in sorted(divisors):
                target = self.metric_defs.get(name)
                if target is not None and self.is_ratio_aggregated(target):
                    raise DerivedMetricAsDivisorError(
                        f"指标 {code!r} 把 aggregation=ratio 的量 {name!r} 当分母："
                        f"比率跨期既不能相加也不能取平均，进了分母算出来的数看着像真的但是错的。"
                        f"请改用可加原子量（revenue / hours / orders 等）做分母。"
                    )

            # --- M2-6 组合层输入约束：比率不许作为公式输入（分母除外，上面已拦）---
            for name in sorted(self._names.get(code, set())):
                if name in divisors:
                    continue
                target = self.metric_defs.get(name)
                if target is None:
                    continue          # 字段没有 aggregation 概念，不在此约束内
                if self.is_ratio_aggregated(target):
                    raise RatioAsInputError(
                        f"指标 {code!r} 把比率 {name!r}（aggregation=ratio）当成公式输入。"
                        f"比率是「算出来」的值，跨期既不能加也不能减；"
                        f"如果确实需要，请用原子量重算（例如 food_cost_rate 改写成 purchase / revenue）。"
                    )

    # ---- 拓扑排序 -------------------------------------------------------- #
    def dependency_graph(self) -> dict[str, set[str]]:
        """code → 它依赖的指标 code 集合。

        注意：必须先取到**全量** metric code 集合再过滤。
        （曾经写成"边建边查 `d in graph`"，结果第一条指标的依赖被整片吞掉，
        拓扑序退化成字母序 —— 恰好也过测试，属于典型的"看着对"的错。）
        """
        codes = set(self.metric_defs)
        graph: dict[str, set[str]] = {}
        for code, md in self.metric_defs.items():
            deps: set[str] = set()
            if md.depends_on:
                deps |= {c.strip() for c in md.depends_on.split(",") if c.strip()}
            deps |= self._names.get(code, set())
            graph[code] = {d for d in deps if d in codes and d != code}
        return graph

    def topo_order(self) -> list[str]:
        graph = self.dependency_graph()
        indegree = {code: len(deps) for code, deps in graph.items()}
        children: dict[str, set[str]] = defaultdict(set)
        for code, deps in graph.items():
            for dep in deps:
                children[dep].add(code)

        ready = sorted([c for c, n in indegree.items() if n == 0])
        order: list[str] = []
        while ready:
            node = ready.pop(0)
            order.append(node)
            for child in sorted(children[node]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
            ready.sort()

        if len(order) != len(graph):
            stuck = sorted(set(graph) - set(order))
            raise CyclicDependencyError(f"指标依赖成环，无法排序：{stuck}")
        return order

    # ---- 原子值聚合（守卫 4：先 Σ 原子） --------------------------------- #
    def _fact_row_value(self, row: FactDaily, code: str) -> float | None:
        if code in TYPED_FIELD_COLUMNS:
            value = getattr(row, code)
        else:
            value = _read_json(row.extra_json).get(code)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _default_or_none(self, fd: FieldDef) -> float | None:
        raw = fd.default_value
        if raw is None or str(raw).strip() == "":
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def aggregate_atoms(self, shop_id: int, start: date, end: date) -> dict[str, float | None]:
        atoms: dict[str, float | None] = {}

        day_fields = [fd for fd in self.field_defs.values() if (fd.scope or "day") == "day"]
        month_fields = [fd for fd in self.field_defs.values() if (fd.scope or "day") == "month"]

        # ---- 日口径：SUM（不是 AVG）----
        rows = self.session.scalars(
            select(FactDaily)
            .where(
                FactDaily.shop_id == shop_id,
                FactDaily.period_type == "day",
                FactDaily.stat_date >= start,
                FactDaily.stat_date <= end,
            )
            .order_by(FactDaily.stat_date)
        ).all()

        for fd in day_fields:
            values = [v for v in (self._fact_row_value(r, fd.code) for r in rows) if v is not None]
            if values:
                atoms[fd.code] = round(sum(values), 6)
            else:
                # 所有天都没填这个字段 → 缺失。允许 field_def 给默认值
                atoms[fd.code] = self._default_or_none(fd)

        # ---- 月口径：取锚点月（查询 end 所在月）那一行 ----
        anchor = month_first_day(end)
        month_row = self.session.scalars(
            select(FactDaily).where(
                FactDaily.shop_id == shop_id,
                FactDaily.period_type == "month",
                FactDaily.stat_date == anchor,
            )
        ).first()

        for fd in month_fields:
            value = self._fact_row_value(month_row, fd.code) if month_row is not None else None
            atoms[fd.code] = value if value is not None else self._default_or_none(fd)

        # ---- 引擎注入项：从 shifts 现算 ----
        range_shifts = self.session.scalars(
            select(Shift).where(
                Shift.shop_id == shop_id,
                Shift.work_date >= start,
                Shift.work_date <= end,
            )
        ).all()
        atoms["hours"] = self._sum_hours(range_shifts)
        atoms["labor_cost"] = self._sum_labor(range_shifts)

        month_shifts = self.session.scalars(
            select(Shift).where(
                Shift.shop_id == shop_id,
                Shift.work_date >= anchor,
                Shift.work_date <= month_last_day(anchor),
            )
        ).all()
        atoms["labor_m"] = self._sum_labor(month_shifts)

        # ---- M3-0：锚点月的营业额（日口径字段在月尺度上的聚合）----
        month_day_rows = self.session.scalars(
            select(FactDaily).where(
                FactDaily.shop_id == shop_id,
                FactDaily.period_type == "day",
                FactDaily.stat_date >= anchor,
                FactDaily.stat_date <= month_last_day(anchor),
            )
        ).all()
        revenue_m = self._sum_field(month_day_rows, "revenue")
        atoms["revenue_m"] = revenue_m
        atoms["purchase_m"] = self._sum_field(month_day_rows, "purchase")

        # 月化营业额 = **区间**日均营业额 × 30。
        #
        # ⚠️ 这里刻意用"区间日均"而不是"锚点月的日均"：任务书 §2 M3-0 给的方案 (a)
        #    `labor_m_full / (revenue_avg × 30)` 里的 `revenue_avg` 是**区间**日均
        #    （与报告里显示的"日均营业额"同一个数）。两者在淡旺季差别大的店上会差很多：
        #    本案 Y3 的锚点月是 9 月（淡季），用锚点月算是 28.9%，用区间日均算是 26.5%
        #    —— 后者才是 docs/01 §4 缺陷 2 的那个 26.5%。口径必须按任务书来。
        #
        # 为什么 ×30 之后仍然标 additive：它是**金额**（元），不是无量纲的率。
        # 分母位置放金额是名正言顺的；放比率才该被 M2-6 拦。
        period_revenue = self._sum_field(rows, "revenue")
        if period_revenue is not None and rows:
            atoms["revenue_m_avg"] = round(period_revenue / len(rows) * 30, 6)
        else:
            atoms["revenue_m_avg"] = None

        # ---- M3-3 / M4-4：账实差异（从 stocktakes / receipts / scraps 三类事实聚合）----
        # 注意这两个是**区间口径**（跟查询区间走），不是月口径 ——
        # 差异要能按"这次盘点覆盖的那段时间"算。
        #
        # M4-4 之后账面数量由系统推算（stock_service 是唯一实现，报告明细也调它）。
        from .stock_service import stock_amounts

        amounts = stock_amounts(self.session, shop_id, start, end)
        atoms["stock_book_amount"] = amounts["book"]
        atoms["stock_actual_amount"] = amounts["actual"]
        atoms["scrap_amount"] = amounts["scrap"]
        # M6（docs/06 §0 裁定 7）：可解释损耗率的分子。
        # 与 scrap_amount 同源同区间，但只算"已登记且通过审核"的那部分。
        atoms["explained_loss_m"] = amounts.get("explained")

        return atoms

    def _sum_field(self, rows: Iterable[FactDaily], code: str) -> float | None:
        values = [v for v in (self._fact_row_value(r, code) for r in rows) if v is not None]
        if not values:
            return None
        return round(sum(values), 6)

    @staticmethod
    def _sum_hours(shifts: Iterable[Shift]) -> float | None:
        rows = list(shifts)
        if not rows:
            return None
        return round(sum(float(s.hours) for s in rows), 6)

    def _sum_labor(self, shifts: Iterable[Shift]) -> float | None:
        rows = list(shifts)
        if not rows:
            return None
        default_rate = self._shop_default_rate(rows[0].shop_id)
        total = 0.0
        for s in rows:
            rate = s.hourly_rate if s.hourly_rate is not None else default_rate
            total += float(s.hours) * float(rate)
        return round(total, 6)

    def _shop_default_rate(self, shop_id: int) -> float:
        from .models import Shop  # 局部导入，避免模块级循环

        shop = self.session.get(Shop, shop_id)
        return float(shop.default_hourly_rate) if shop is not None else 0.0

    # ---- 主入口 ---------------------------------------------------------- #
    def compute(
        self,
        shop_id: int,
        start: date | str,
        end: date | str,
        *,
        persist: bool = True,
    ) -> dict[str, float | None]:
        """施工单 3.5 的五步：聚合 → 读表 → 拓扑求值 → 写缓存 → 返回。

        返回值的范围（对施工单 §3.5 第 5 步的一处明确化）：
            返回**参与计算的完整值集** = 原子字段（revenue / purchase / …）
            + 引擎注入量（hours / labor_cost / labor_m）+ 全部派生指标。

        为什么不只返回指标：报告第一行要给老板看"营业额 4200、人工 224、食材 1100"，
        这三样里只有人工是指标。若只返回指标，报告就得再去查一遍事实表，
        出现两个数字来源 —— 一旦口径分叉，报告里的数和对账时看的数就会不一样。
        """
        start_d, end_d = as_date(start), as_date(end)
        if start_d > end_d:
            start_d, end_d = end_d, start_d

        self.validate()

        ctx: dict[str, float | None] = self.aggregate_atoms(shop_id, start_d, end_d)
        values: dict[str, float | None] = {
            code: (round(value, 6) if isinstance(value, float) else value)
            for code, value in ctx.items()
        }

        for code in self.topo_order():
            md = self.metric_defs[code]
            # 有公式就算公式（与 layer 无关）；没公式就是直接取数（field_def 或引擎注入）
            if (md.formula or "").strip():
                value = FormulaParser.evaluate(self._trees[code], ctx)
            else:
                value = ctx.get(code)
            if isinstance(value, float):
                value = round(value, 6)
            values[code] = value
            ctx[code] = value

        if persist:
            self._persist(shop_id, start_d, end_d, values)
        return values

    def evaluate_on(self, atoms: Mapping[str, float | None]) -> dict[str, float | None]:
        """在一份**给定的原子量**上跑完整套公式（不碰数据库）。

        M4-6 多店汇总就是靠它：先把 N 家店的同名原子量**相加**，再跑同一套公式 ——
        结果才是"合并后的那家店"。

        **绝不允许对各店比率取平均**：三家店营业额 100/300/500、人工 25/60/100 时，
        正确汇总是 185/900 = 20.56%；按"三家比率取平均"会得到 21.67% —— 那是错数。
        """
        self.validate()
        ctx: dict[str, float | None] = {
            code: (round(v, 6) if isinstance(v, float) else v) for code, v in atoms.items()
        }
        values = dict(ctx)
        for code in self.topo_order():
            md = self.metric_defs[code]
            if (md.formula or "").strip():
                value = FormulaParser.evaluate(self._trees[code], ctx)
            else:
                value = ctx.get(code)
            if isinstance(value, float):
                value = round(value, 6)
            values[code] = value
            ctx[code] = value
        return values

    def sum_atoms(
        self, shop_ids: Sequence[int], start: date | str, end: date | str
    ) -> dict[str, float | None]:
        """把多家店的原子量**逐项相加**（None 视为"这家店没这个数"，跳过）。"""
        start_d, end_d = as_date(start), as_date(end)
        merged: dict[str, float] = {}
        seen: set[str] = set()
        for shop_id in shop_ids:
            atoms = self.aggregate_atoms(shop_id, start_d, end_d)
            for code, value in atoms.items():
                seen.add(code)
                if value is None:
                    continue
                merged[code] = merged.get(code, 0.0) + float(value)
        return {
            code: (round(merged[code], 6) if code in merged else None)
            for code in seen
        }

    def _persist(
        self, shop_id: int, start: date, end: date, values: Mapping[str, float | None]
    ) -> None:
        self.session.execute(
            delete(MetricValue).where(
                MetricValue.shop_id == shop_id,
                MetricValue.period_start == start.isoformat(),
                MetricValue.period_end == end.isoformat(),
            )
        )
        now = datetime.now().isoformat(timespec="seconds")
        for code, value in values.items():
            self.session.add(
                MetricValue(
                    shop_id=shop_id,
                    period_start=start.isoformat(),
                    period_end=end.isoformat(),
                    metric_code=code,
                    value=value,
                    computed_at=now,
                )
            )
        self.session.flush()

    # ---- 辅助：哪些指标算不出、为什么 ------------------------------------ #
    def blocked_reasons(self, values: Mapping[str, float | None]) -> list[dict[str, str]]:
        """把值为 None 的**比率型**指标翻译成"还缺哪个数"的大白话提示。

        ## M7 WP-2 / D1（docs/08 §5.1）：内部字段名绝不给老板看

        曾实测出「人力成本率还算不出，缺：labor_cost、营业额」——
        翻译表只覆盖了 7 个名字，其余回退成 `hints.get(name, name)` 的裸字段名。
        这违反 M0 硬规矩（对老板不出现术语）。

        三层翻译，**没有一层回退到原始代码**：
          1. `hints` 显式表（人工措辞最准）
          2. `field_def.label` / `metric_def.name`（注册表里的中文名）
          3. `"某项数据"`（最后的兜底）
        这样将来谁再加指标忘了更新 hints，老板看到的也是中文，不是 `xxx_yyy`。
        """
        hints = {
            "revenue": "营业额",
            "purchase": "买菜钱",
            "orders": "订单数",
            "hours": "工时（今天谁上了多久）",
            "labor_m": "月人工（要从排班算）",
            "rent_m": "月房租",
            "utility_m": "月水电",
            # M7 WP-2 补全（实测泄露的 5 个）
            "labor_cost": "排班人工（先记今天谁上班）",
            "labor_m_full": "员工月薪（在「我的店」里填）",
            "revenue_m_avg": "营业额（要多记几天）",
            "stock_book_amount": "盘点（还没做盘点）",
            "stock_variance_amount": "盘点（还没做盘点）",
        }

        def boss_name(name: str) -> str:
            """一个内部名字 → 老板能看的话（三层，绝不回退裸名）。"""
            if name in hints:
                return hints[name]
            fd = self.field_defs.get(name)
            if fd and fd.label:
                return fd.label
            md = self.metric_defs.get(name)
            if md and md.name:
                return md.name
            return "某项数据"

        out: list[dict[str, str]] = []
        for code, value in values.items():
            if value is not None:
                continue
            md = self.metric_defs.get(code)
            if md is None or md.layer == "atomic":
                continue
            # 去重：两个内部名可能翻成同一句话（如 stock_book_amount /
            # stock_variance_amount 都是"盘点（还没做盘点）"），
            # 老板不需要看到"缺：盘点、盘点"。
            missing: list[str] = []
            for name in sorted(self._names.get(code, set())):
                if values.get(name) is not None:
                    continue
                phrase = boss_name(name)
                if phrase not in missing:
                    missing.append(phrase)
            if missing:
                out.append({"code": code, "missing": "、".join(missing)})
        return out
