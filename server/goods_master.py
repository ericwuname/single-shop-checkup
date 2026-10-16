"""商品主数据治理（M4-3 / M4-10 / M4-11 / M4-12）。

本模块装三件事，**都是"数据治理"而不是"业务计算"**，所以从路由里拿出来：

1. **分类树种子**（M4-3）：9 个一级 + 44 个二级。只拆两级 ——
   三级分类对夫妻店是灾难，店员是零意识，选不动。
2. **编码规则**（M4-10）：`[大类两位]-[中类两位]-[序号三位]-[规格两位]`，例 `02-01-001-01`。
   编码里**不写供应商、不写门店**（供应商会换，门店会开分店，写进去就是埋雷）。
3. **查重与合并**（M4-12）：三步法 —— 模糊匹配找疑似 → 人工确认 → 创建前强制查重。
   查重必须**和合并成对**：只提醒不合并，等于发现了也修不了。

## 一条红线（写进代码，不可绕过）

**编码一旦被业务数据引用，就永不修改，只能停用后新建。**
直接改编码会让历史单据里的旧编码变成孤儿，所有按编码聚合的报表断裂，且**不可逆**。
实现见 `block_code_change()`，由 PATCH /api/goods 无条件调用。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Goods, GoodsAlias, Purchase, Receipt, Scrap, Stocktake

# =========================================================================== #
# M4-3 · 分类树（依据：宜昌市教育局食堂食材分类细则 + 简道云进销存实践 + 用户列举）
# =========================================================================== #
#: (一级码, 一级名, [(二级名), ...])
CATEGORY_TREE: list[tuple[str, str, list[str]]] = [
    ("01", "蔬菜类", ["叶菜", "根茎", "瓜果", "菌菇", "鲜豆", "干菜"]),
    ("02", "肉禽蛋类", ["猪肉", "牛羊肉", "禽肉", "蛋类", "肉制品"]),
    ("03", "水产海鲜类", ["鱼类", "虾蟹", "贝类", "水产制品"]),
    ("04", "粮油干货类", ["米面", "食用油", "杂粮", "干货", "豆制品"]),
    ("05", "调料类", ["酱料", "干粉", "香辛料", "糖盐", "食品添加剂"]),
    ("06", "酒水饮料类", ["酒类", "碳酸饮料", "果汁", "瓶装水", "茶咖原料"]),
    ("07", "包材类", ["餐盒", "一次性餐具", "杯具", "包装袋", "其他包材"]),
    ("08", "清洁类", ["清洁剂", "清洁工具", "消杀用品", "垃圾袋"]),
    ("09", "营销物料类", ["菜单菜牌", "宣传品", "会员物料", "门店装饰", "其他"]),
]


def build_category_seeds() -> list[dict[str, Any]]:
    """摊平成可直接插库的行：一级 level=1（parent_code 为空）、二级 level=2。"""
    rows: list[dict[str, Any]] = []
    for i, (code, name, children) in enumerate(CATEGORY_TREE, start=1):
        rows.append(
            {
                "code": code,
                "parent_code": None,
                "name": name,
                "level": 1,
                "sort_order": i,
                "is_custom": 0,
                "enabled": 1,
            }
        )
        for j, child in enumerate(children, start=1):
            rows.append(
                {
                    "code": f"{code}-{j:02d}",
                    "parent_code": code,
                    "name": child,
                    "level": 2,
                    "sort_order": i * 100 + j,
                    "is_custom": 0,
                    "enabled": 1,
                }
            )
    return rows


CATEGORY_SEEDS: list[dict[str, Any]] = build_category_seeds()

#: 编码 → 名称，便于拼 category_path
CATEGORY_NAME = {r["code"]: r["name"] for r in CATEGORY_SEEDS}


def category_path(code: str | None) -> str | None:
    """'02-01' → '肉禽蛋类 / 猪肉'。"""
    if not code:
        return None
    parent = code.split("-")[0]
    parts = [CATEGORY_NAME.get(parent), CATEGORY_NAME.get(code)]
    return " / ".join(p for p in parts if p) or None


# =========================================================================== #
# M4-5 · 三年分类占比曲线（用来解释"食材成本率为什么从 30% 涨到 41%"）
# =========================================================================== #
#: 一级分类 → (Y1, Y2, Y3) 占比。合计 100%。
#: 设计意图很明确：**肉禽蛋占比逐年上升是推高成本的主因** ——
#: 这样分类维度才能"解释"总食材率的变化，而不只是把数字拆开好看。
CATEGORY_SHARE_CURVE: dict[str, tuple[float, float, float]] = {
    "01": (0.28, 0.25, 0.22),   # 蔬菜 下降
    "02": (0.30, 0.34, 0.38),   # 肉禽蛋 **上升 → 主因**
    "03": (0.08, 0.08, 0.07),
    "04": (0.12, 0.12, 0.12),
    "05": (0.10, 0.10, 0.11),
    "06": (0.08, 0.07, 0.06),
    "07": (0.02, 0.02, 0.02),   # 包材
    "08": (0.01, 0.01, 0.01),   # 清洁
    "09": (0.01, 0.01, 0.01),   # 营销物料
}


# =========================================================================== #
# M4-10 / M4-11 · 编码规则与商品种子
# =========================================================================== #
CODE_RE = re.compile(r"^\d{2}-\d{2}-\d{3}-\d{2}$")

#: (名称, 规格, 二级分类, 库存单位, 进货单位, 1进货=N库存, 领用单位, 1库存=N领用,
#:  单价元/库存单位, 是否称重, ABC)
GOODS_SEEDS: list[tuple] = [
    # ---- 01 蔬菜类（叶菜/根茎/瓜果/菌菇/鲜豆/干菜）----
    ("小油菜", "散装", "01-01", "斤", "斤", 1.0, "份", 2.0, 3.2, 1, "A"),
    ("生菜", "散装", "01-01", "斤", "斤", 1.0, "份", 2.0, 3.6, 1, "B"),
    ("土豆", "散装", "01-02", "斤", "袋", 20.0, "份", 5.0, 2.4, 1, "A"),
    ("胡萝卜", "散装", "01-02", "斤", "袋", 10.0, "份", 4.0, 2.8, 1, "B"),
    ("青椒", "散装", "01-03", "斤", "斤", 1.0, "份", 4.0, 4.5, 1, "B"),
    ("西红柿", "散装", "01-03", "斤", "斤", 1.0, "份", 3.0, 4.2, 1, "A"),
    ("香菇", "散装", "01-04", "斤", "斤", 1.0, "份", 5.0, 8.5, 1, "B"),
    ("干木耳", "袋装 250g", "01-06", "斤", "袋", 2.0, "份", 10.0, 32.0, 0, "C"),
    # ---- 02 肉禽蛋类（猪肉/牛羊肉/禽肉/蛋类/肉制品）----
    ("五花肉", "散装", "02-01", "斤", "斤", 1.0, "份", 5.0, 16.5, 1, "A"),
    ("前腿肉", "散装", "02-01", "斤", "斤", 1.0, "份", 5.0, 14.0, 1, "A"),
    ("肋排", "散装", "02-01", "斤", "斤", 1.0, "份", 4.0, 28.0, 1, "A"),
    ("鲜牛肉", "散装", "02-02", "斤", "斤", 1.0, "份", 4.0, 42.0, 1, "A"),
    ("鸡腿肉", "散装", "02-03", "斤", "箱", 20.0, "份", 4.0, 12.8, 1, "A"),
    ("鸡蛋", "散装", "02-04", "斤", "箱", 30.0, "个", 16.0, 5.6, 1, "A"),
    ("香肠", "袋装 500g", "02-05", "斤", "袋", 1.0, "份", 8.0, 26.0, 0, "C"),
    # ---- 03 水产海鲜类 ----
    ("巴沙鱼柳", "袋装 1kg", "03-01", "斤", "袋", 2.0, "份", 4.0, 13.5, 0, "A"),
    ("基围虾", "散装", "03-02", "斤", "斤", 1.0, "份", 5.0, 38.0, 1, "A"),
    ("干海带结", "袋装 500g", "03-04", "斤", "袋", 1.0, "份", 10.0, 18.0, 0, "C"),
    # ---- 04 粮油干货类（米面/食用油/杂粮/干货/豆制品）----
    ("珍珠米", "袋装 25kg", "04-01", "斤", "袋", 50.0, "份", 3.0, 2.6, 1, "A"),
    ("高筋面粉", "袋装 25kg", "04-01", "斤", "袋", 50.0, "份", 3.0, 2.2, 1, "A"),
    # ⚠ 同料不同规格：大豆油 5L / 20L —— 必须拆成两个编码
    ("大豆油", "5L/桶", "04-02", "L", "桶", 5.0, "勺", 50.0, 9.4, 0, "A"),
    ("大豆油", "20L/桶", "04-02", "L", "桶", 20.0, "勺", 50.0, 8.9, 0, "A"),
    ("玉米淀粉", "袋装 25kg", "04-03", "斤", "袋", 50.0, "勺", 60.0, 2.9, 0, "C"),
    ("干辣椒", "袋装 1kg", "04-04", "斤", "袋", 2.0, "勺", 100.0, 24.0, 0, "C"),
    ("老豆腐", "散装", "04-05", "斤", "斤", 1.0, "份", 4.0, 3.0, 1, "B"),
    # ---- 05 调料类 ----
    ("生抽", "1.9L/瓶", "05-01", "L", "瓶", 1.9, "勺", 60.0, 7.5, 0, "B"),
    ("蚝油", "2.3kg/瓶", "05-01", "斤", "瓶", 4.6, "勺", 40.0, 9.2, 0, "B"),
    ("豆瓣酱", "袋装 5kg", "05-01", "斤", "袋", 10.0, "勺", 40.0, 6.4, 0, "B"),
    ("十三香", "袋装 45g", "05-03", "斤", "袋", 0.09, "勺", 200.0, 28.0, 0, "C"),
    ("白砂糖", "袋装 50kg", "05-04", "斤", "袋", 100.0, "勺", 100.0, 3.4, 0, "C"),
    ("食用盐", "袋装 1kg", "05-04", "斤", "袋", 2.0, "勺", 200.0, 1.6, 0, "C"),
    # ---- 06 酒水饮料类 ----
    ("可乐", "330ml/罐", "06-02", "罐", "箱", 24.0, "罐", 1.0, 2.6, 0, "B"),
    ("可乐", "2L/瓶", "06-02", "瓶", "箱", 6.0, "杯", 8.0, 6.8, 0, "B"),
    ("农夫山泉", "550ml/瓶", "06-04", "瓶", "箱", 24.0, "瓶", 1.0, 1.5, 0, "C"),
    ("鲜橙汁", "1L/瓶", "06-03", "L", "箱", 12.0, "杯", 4.0, 8.6, 0, "B"),
    ("绿茶茶包", "袋装 500g", "06-05", "斤", "袋", 1.0, "勺", 150.0, 42.0, 0, "C"),
    # ---- 07 包材类 ----
    ("一次性餐盒", "750ml/个", "07-01", "个", "箱", 300.0, "个", 1.0, 0.62, 0, "B"),
    ("一次性筷子", "100双/包", "07-02", "双", "包", 100.0, "双", 1.0, 0.09, 0, "C"),
    ("打包袋", "中号/个", "07-04", "个", "包", 100.0, "个", 1.0, 0.14, 0, "C"),
    # ---- 08 清洁类 ----
    ("洗洁精", "5kg/桶", "08-01", "斤", "桶", 10.0, "勺", 50.0, 3.8, 0, "C"),
    ("垃圾袋", "大号/个", "08-04", "个", "包", 50.0, "个", 1.0, 0.35, 0, "C"),
    # ---- 09 营销物料类 ----
    ("菜单菜牌", "A4/张", "09-01", "张", "张", 1.0, "张", 1.0, 2.0, 0, "C"),
    ("门店海报", "A2/张", "09-04", "张", "张", 1.0, "张", 1.0, 6.0, 0, "C"),
]

#: 异名同物：别名 → 正名（M4-12 别名表）。靠相似度**查不出来**，必须显式维护。
ALIAS_SEEDS: list[tuple[str, str]] = [
    ("西红柿", "番茄"),
    ("洋柿子", "番茄"),
    ("马铃薯", "土豆"),
    ("土豆仔", "土豆"),
]

#: 查重用例（M4-5 要求造 3 条疑似重复，用于验证弹窗）
DUPLICATE_PROBES: list[tuple[str, str]] = [
    ("可乐", "330ml/罐"),          # 与已有"可乐 330ml/罐"完全同名同规格 → 必须确认
    ("可口可乐", "330ml/罐"),      # 与"可乐"高度相似 → 必须确认
    ("大油豆", "5L/桶"),           # 与"大豆油"字形相近 → 提示
]


def next_code(session: Session, shop_id: int, category_code: str, spec_code: str = "01") -> str:
    """生成下一个商品编码：`[大类]-[中类]-[序号三位]-[规格两位]`。"""
    major, middle = category_code.split("-")[:2]
    prefix = f"{major}-{middle}-"
    existing = session.scalars(
        select(Goods.code).where(Goods.shop_id == shop_id, Goods.code.like(f"{prefix}%"))
    ).all()
    seq = 1
    for code in existing:
        parts = code.split("-")
        if len(parts) == 4 and parts[2].isdigit():
            seq = max(seq, int(parts[2]) + 1)
    return f"{prefix}{seq:03d}-{spec_code}"


# =========================================================================== #
# M4-12 · 查重（三步法的第 1、3 步）
# =========================================================================== #
DEFAULT_DUPLICATE_THRESHOLD = 0.75


def _norm(text: str | None) -> str:
    return re.sub(r"[\s\-_/（）()、,，.]+", "", (text or "").lower())


def _initials(text: str) -> str:
    """汉字首字母，用于"k l → 可乐"式检索（中文输入慢，找不到料就自己新建一个是头号成因）。"""
    try:
        from pypinyin import Style, lazy_pinyin

        return "".join(lazy_pinyin(text, style=Style.FIRST_LETTER))
    except Exception:                        # pragma: no cover - 没装 pypinyin 时降级
        return ""


def name_similarity(a: str, b: str) -> float:
    """名称相似度 0–1（去符号、去大小写后比），再对"包含关系"给一点加成。"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    if na in nb or nb in na:
        ratio = max(ratio, 0.8)              # "可乐" vs "可口可乐" → 给包含加成
    return round(ratio, 4)


def check_duplicates(
    session: Session,
    *,
    shop_id: int,
    name: str,
    spec: str | None = None,
    threshold: float = DEFAULT_DUPLICATE_THRESHOLD,
) -> tuple[list[dict[str, Any]], bool]:
    """返回（疑似清单，是否必须人工确认）。

    三步法的第 1 步 + 第 3 步：
      * 名称相似度 ≥ 阈值 → 提示；
      * **同名且同规格** → 高度相似，`must_confirm=True`（不确认不许建）；
      * **同名但规格不同** → 提示但**不算重复**（规格不同就是两个 SKU），不算 must_confirm；
      * 命中别名 → 提示。
    """
    goods = session.scalars(
        select(Goods).where(Goods.shop_id == shop_id, Goods.enabled == 1)
    ).all()
    aliases = {
        row.alias: row.goods_id
        for row in session.scalars(select(GoodsAlias).where(GoodsAlias.shop_id == shop_id)).all()
    }

    q_norm = _norm(name)
    candidates: list[dict[str, Any]] = []
    must_confirm = False

    for row in goods:
        score = name_similarity(name, row.name)
        reasons: list[str] = []
        same_name = _norm(row.name) == q_norm
        same_spec = _norm(row.spec) == _norm(spec)

        if same_name and same_spec:
            score = max(score, 0.99)
            reasons.append("同名同规格")
            must_confirm = True
        elif same_name:
            score = max(score, 0.9)
            reasons.append("同名不同规格（规格不同就是两个 SKU，不算重复，但要确认没选错）")
        elif score >= threshold:
            reasons.append("名称高度相似")

        if reasons and score >= min(threshold, 0.9):
            candidates.append(
                {
                    "goods_id": row.id,
                    "code": row.code,
                    "name": row.name,
                    "spec": row.spec,
                    "unit": row.unit,
                    "category_path": row.category_path,
                    "score": round(score, 4),
                    "reason": "；".join(reasons),
                }
            )

    # 命中别名表（"可口可乐"这类异名，靠相似度不一定够）
    for alias, goods_id in aliases.items():
        if _norm(alias) == q_norm:
            target = session.get(Goods, goods_id)
            if target is not None and all(c["goods_id"] != target.id for c in candidates):
                candidates.append(
                    {
                        "goods_id": target.id,
                        "code": target.code,
                        "name": target.name,
                        "spec": target.spec,
                        "unit": target.unit,
                        "category_path": target.category_path,
                        "score": 0.95,
                        "reason": f"命中别名「{alias}」",
                    }
                )

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates, must_confirm


def search_goods(session: Session, shop_id: int, q: str, limit: int = 20) -> list[Goods]:
    """检索：名称子串 / 规格 / 编码 / **拼音首字母**（"kl" → 可乐）/ 别名。"""
    q = (q or "").strip()
    rows = session.scalars(
        select(Goods).where(Goods.shop_id == shop_id, Goods.enabled == 1).order_by(Goods.code)
    ).all()
    aliases: dict[int, list[str]] = {}
    for row in session.scalars(select(GoodsAlias).where(GoodsAlias.shop_id == shop_id)).all():
        aliases.setdefault(row.goods_id, []).append(row.alias)

    if not q:
        return rows[:limit]

    ql = q.lower()
    q_initials = _initials(q)
    hits: list[Goods] = []
    for row in rows:
        haystack = " ".join(
            filter(None, [row.name, row.spec, row.code, row.category_path, *aliases.get(row.id, [])])
        )
        if ql in haystack.lower():
            hits.append(row)
            continue
        # 拼音：整体首字母，或任一"词"的首字母前缀
        if q_initials and (
            _initials(row.name).startswith(q_initials) or q_initials in _initials(row.name)
        ):
            hits.append(row)
            continue
        if aliases.get(row.id) and any(ql in a.lower() for a in aliases[row.id]):
            hits.append(row)
    return hits[:limit]


# =========================================================================== #
# M4-12 · 合并（三步法的第 2 步）+ 红线
# =========================================================================== #
#: 引用过商品的业务表（用于判定"编码能不能改"和"合并要搬哪些历史数据"）
REFERENCING_MODELS = (Purchase, Receipt, Stocktake, Scrap)


def reference_count(session: Session, goods_id: int) -> int:
    total = 0
    for model in REFERENCING_MODELS:
        column = getattr(model, "goods_id")
        total += int(
            session.scalar(select(func.count()).select_from(model).where(column == goods_id)) or 0
        )
    return total


def block_code_change(session: Session, goods: Goods, new_code: str) -> None:
    """🔴 红线：编码被业务数据引用过 → **拒绝改**，只能停用后新建。

    直接改编码会让历史单据里的旧编码变成孤儿、按编码聚合的报表断裂，**不可逆**。
    """
    if new_code == goods.code:
        return
    if goods.is_referenced or reference_count(session, goods.id) > 0:
        raise ValueError(
            f"商品 {goods.code} 已被单据引用，编码不可修改（改了历史数据会变孤儿且不可逆）。"
            f"请停用后新建一个编码。"
        )


def merge_goods(
    session: Session, *, shop_id: int, source_ids: Sequence[int], target_id: int
) -> dict[str, Any]:
    """把副码合并到主码：历史单据搬到主码 → 副码写别名 → 副码停用 + 指向主码。"""
    target = session.get(Goods, target_id)
    if target is None or target.shop_id != shop_id:
        raise ValueError(f"主码 {target_id} 不存在")

    merged: list[str] = []
    moved = 0
    for sid in source_ids:
        if sid == target_id:
            continue
        src = session.get(Goods, sid)
        if src is None or src.shop_id != shop_id:
            continue
        # 1) 历史单据归集到主码（这正是"合并"的意义：报表不再分裂）
        for model in REFERENCING_MODELS:
            rows = session.scalars(
                select(model).where(getattr(model, "goods_id") == sid)
            ).all()
            for row in rows:
                row.goods_id = target_id
                moved += 1
        # 2) 副码的名字进别名表，以后按副名检索/归集也能找到主码
        exists = session.scalars(
            select(GoodsAlias).where(
                GoodsAlias.shop_id == shop_id, GoodsAlias.alias == src.name
            )
        ).first()
        if exists is None:
            session.add(
                GoodsAlias(
                    shop_id=shop_id,
                    goods_id=target_id,
                    alias=src.name,
                    note=f"由合并 {src.code} 生成",
                )
            )
        # 3) 停用 ≠ 删除：副码留着指向主码，历史可查
        src.merged_into_id = target_id
        src.enabled = 0
        merged.append(src.code)

    target.is_referenced = 1 if reference_count(session, target_id) > 0 else target.is_referenced
    session.flush()
    return {
        "target_id": target_id,
        "target_code": target.code,
        "merged": merged,
        "moved_records": moved,
    }


def mark_referenced(session: Session, goods_id: int) -> None:
    """任何写单据的动作都要调用它 —— 引用过一次，编码就永远锁住。"""
    goods = session.get(Goods, goods_id)
    if goods is not None and not goods.is_referenced:
        goods.is_referenced = 1


# =========================================================================== #
# 播种（幂等）
# =========================================================================== #
def seed_categories(session: Session) -> dict[str, str]:
    """分类主数据是**全局共享**的（门店间共用一套），所以不带 shop_id。"""
    from .models import GoodsCategory

    result: dict[str, str] = {}
    for seed in CATEGORY_SEEDS:
        row = session.get(GoodsCategory, seed["code"])
        if row is None:
            session.add(GoodsCategory(**seed))
            result[seed["code"]] = "inserted"
            continue
        changed = False
        for col in ("parent_code", "name", "level", "sort_order", "enabled"):
            if getattr(row, col) != seed.get(col):
                setattr(row, col, seed[col])
                changed = True
        # is_custom 不动：老板改过的预置分类不该被播种覆盖回 0
        result[seed["code"]] = "updated" if changed else "unchanged"
    session.flush()
    return result


def seed_goods_for_shop(session: Session, shop_id: int) -> dict[str, int]:
    """按 GOODS_SEEDS 铺商品（按 shop_id + 名称 + 规格 幂等）。"""
    existing = {
        (row.name, row.spec or "")
        for row in session.scalars(select(Goods).where(Goods.shop_id == shop_id)).all()
    }
    inserted = 0
    for idx, row in enumerate(GOODS_SEEDS, start=1):
        (name, spec, cat2, stock_unit, buy_unit, conv, use_unit, s2u,
         price, weighed, abc) = row
        if (name, spec) in existing:
            continue
        code = next_code(session, shop_id, cat2, spec_code=f"{(idx % 99) + 1:02d}")
        session.add(
            Goods(
                shop_id=shop_id,
                code=code,
                name=name,
                spec=spec,
                spec_code=f"{(idx % 99) + 1:02d}",
                category_code=cat2,
                category_path=category_path(cat2),
                unit=stock_unit,
                purchase_unit=buy_unit,
                usage_unit=use_unit,
                unit_conversion=conv,
                stock_to_usage=s2u,
                is_weighed=weighed,
                unit_price=price,
                abc_class=abc,
            )
        )
        inserted += 1
    session.flush()
    return {"goods_inserted": inserted, "goods_total": len(GOODS_SEEDS)}


def seed_aliases(session: Session, shop_id: int) -> int:
    """异名同物。靠相似度查不出来，必须显式维护。"""
    from .models import GoodsCategory  # noqa: F401 - 保持导入显式

    by_name: dict[str, Goods] = {
        row.name: row
        for row in session.scalars(select(Goods).where(Goods.shop_id == shop_id)).all()
    }
    added = 0
    for alias, canonical in ALIAS_SEEDS:
        target = by_name.get(canonical)
        if target is None:
            continue
        exists = session.scalars(
            select(GoodsAlias).where(GoodsAlias.shop_id == shop_id, GoodsAlias.alias == alias)
        ).first()
        if exists is None:
            session.add(
                GoodsAlias(shop_id=shop_id, goods_id=target.id, alias=alias, note="异名同物")
            )
            added += 1
    session.flush()
    return added
