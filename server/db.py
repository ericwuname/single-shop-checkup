"""数据库连接与初始化（施工单 3.2 / server/db.py）。

设计要点
--------
1. **延迟绑定**：engine / SessionLocal 由 ``init_db()`` 建立。测试可以在导入后
   重新指向临时库，不需要 reload 模块。
2. SQLite 起步（M1），M4 迁 PostgreSQL —— 因此这里不写任何 SQLite 专有 SQL，
   只有 ``connect_args`` / PRAGMA / 列改名三处按方言分支。
3. 建表 = ``Base.metadata.create_all``。M1 明确不引入 Alembic；
   M4 才补一个**最小**的迁移能力（见下方 ``migrate_renames``），
   仍然不引入 Alembic —— 只需要"列改名"这一种，不值得引一整套迁移框架。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import Float, Integer, create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "checkup.db"

_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker] = None

#: 上一次 init_db 实际执行过的列改名（供启动日志/脚本显示）
MIGRATIONS_APPLIED: list[str] = []

#: M4 列改名映射：``表 -> {旧列名: 新列名}``
#:
#: 为什么必须有它：``create_all`` **只建新表，绝不修改已有表**。
#: `goods.conversion_rate` 改名成 `unit_conversion` 之后，老库不会自动跟上，
#: 一查询就是 "no such column" —— 而建表工具对此完全沉默。
COLUMN_RENAMES: dict[str, dict[str, str]] = {
    "goods": {
        # M4-3/M4-11：换算率统一叫 unit_conversion（= 1 进货单位 = N 库存单位）
        "conversion_rate": "unit_conversion",
        # M4-3 消歧：原来的 category 是 A/B/C「ABC 分类」，
        # 而新加的 category_code 是「存货分类树」，两个概念不能共用一个名字
        "category": "abc_class",
    },
}


def sqlite_url_for(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    return "sqlite:///" + path.as_posix()


def resolve_db_url(url: Optional[str] = None) -> str:
    """优先显式入参 > 环境变量 DATABASE_URL > 项目内 data/checkup.db。

    SQLite 文件路径会自动建父目录 —— 否则 `sqlite:///./data/x.db` 在 data/ 不存在时
    报 "unable to open database file"，而这跟业务错误长得太像，不值得让人排查一次。
    """
    if url:
        return _ensure_sqlite_parent(url)
    env = os.environ.get("DATABASE_URL", "").strip()
    if env:
        return _ensure_sqlite_parent(env)
    return sqlite_url_for(DEFAULT_DB_PATH)


def _ensure_sqlite_parent(url: str) -> str:
    if not url.startswith("sqlite:///"):
        return url
    raw = url[len("sqlite:///") :]
    if raw in {"", ":memory:"} or raw.startswith("file:"):
        return url
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return "sqlite:///" + path.as_posix()


def _on_connect(dbapi_conn, _record) -> None:  # pragma: no cover - 由 SQLite 驱动回调
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    # M6-4（docs/06 §5.1）：WAL + 写超时。
    #
    # WAL 让"读不阻塞写、写不阻塞读" —— 晚上 10 点录入高峰，多家店同时写不至于锁死。
    # busy_timeout 是**写超时**：拿不到写锁时最多等 5 秒再报错，
    # 而不是立刻抛 "database is locked"（老板看到的就是"保存失败"）。
    #
    # synchronous=NORMAL 是 WAL 模式的官方推荐组合：断电最多丢最近一笔事务，
    # 但日常写入快一个量级。对"每天三个数"的场景这个取舍是值得的 ——
    # 真要零丢失得上 PostgreSQL（M4 已排到那里）。
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def migrate_renames(engine: Engine) -> list[str]:
    """把老库里的旧列名改成新列名。返回实际执行的改动列表（幂等）。

    只在 SQLite 上做（用 PRAGMA 探测 + `ALTER TABLE ... RENAME COLUMN`，SQLite ≥ 3.25）。
    全新库上表还不存在，create_all 已经按新名建好，这一步自然跳过。
    """
    applied: list[str] = []
    if engine.dialect.name != "sqlite":
        return applied

    with engine.begin() as conn:
        for table, mapping in COLUMN_RENAMES.items():
            rows = conn.exec_driver_sql(f'PRAGMA table_info("{table}")').fetchall()
            if not rows:
                continue                      # 表还不存在 → 无需迁移
            existing = {r[1] for r in rows}
            for old, new in mapping.items():
                # 只在「旧列还在、新列还没有」时改 —— 这样重复跑不会出错
                if old in existing and new not in existing:
                    conn.exec_driver_sql(
                        f'ALTER TABLE "{table}" RENAME COLUMN "{old}" TO "{new}"'
                    )
                    applied.append(f"{table}.{old} -> {new}")
    return applied


def migrate_add_columns(engine: Engine) -> list[str]:
    """把模型里**新加的列**补到老库上。返回实际执行的改动列表（幂等）。

    为什么必须有它：``create_all`` 只建新表，**不会给已有表加列**。
    M4 给 `goods` 一口气加了十列，老库不补就会在第一次查询时报 "no such column"。

    只在 SQLite 上做（SQLite 允许 ADD COLUMN，但要求 NOT NULL 列带默认值）。
    """
    applied: list[str] = []
    if engine.dialect.name != "sqlite":
        return applied

    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue                     # 新表交给 create_all
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = (
                    f'ALTER TABLE "{table.name}" ADD COLUMN '
                    f'"{col.name}" {col.type.compile(engine.dialect)}'
                )
                scalar_default = (
                    col.default.arg
                    if col.default is not None and getattr(col.default, "is_scalar", False)
                    else None
                )
                if scalar_default is not None:
                    literal = (
                        f"'{scalar_default}'"
                        if isinstance(scalar_default, str)
                        else repr(scalar_default)
                    )
                    ddl += f" DEFAULT {literal}"
                elif col.nullable:
                    ddl += " DEFAULT NULL"
                elif isinstance(col.type, (Integer, Float)):
                    ddl += " DEFAULT 0"          # SQLite 的 NOT NULL 列必须给默认值
                else:
                    ddl += " DEFAULT ''"
                if not col.nullable:
                    ddl += " NOT NULL"
                conn.exec_driver_sql(ddl)
                applied.append(f"{table.name}.{col.name} (added)")
    return applied


def migrate_add_unique_indexes(engine: Engine) -> list[str]:
    """把模型里**新加的唯一索引**补到老库上（`create_all` 同样不补）。

    M5 自查第二轮 G3 需要它：`bom_variance` 加唯一约束是后来决定的，
    而 M5 第一轮已经在真库 `data/checkup.db` 里建过这张表了 ——
    不补索引的话，老库上这条约束形同不存在（重复提交照样能写进去）。
    SQLite 支持 `CREATE UNIQUE INDEX IF NOT EXISTS`，所以是幂等的。
    """
    applied: list[str] = []
    if engine.dialect.name != "sqlite":
        return applied

    #: (索引名, 表名, 列名列表) —— 与 models.py 的 __table_args__ 保持一致
    UNIQUE_INDEXES = (
        ("uq_bom_variance_day_dish_goods", "bom_variance",
         ("shop_id", "biz_date", "dish_name", "goods_id")),
    )
    inspector = inspect(engine)
    with engine.begin() as conn:
        for name, table, columns in UNIQUE_INDEXES:
            if not inspector.has_table(table):
                continue                     # 表还没建 → create_all 会带上约束
            existing = {ix["name"] for ix in inspector.get_indexes(table)}
            if name in existing:
                continue
            cols = ", ".join(f'"{c}"' for c in columns)
            conn.exec_driver_sql(
                f'CREATE UNIQUE INDEX IF NOT EXISTS "{name}" ON "{table}" ({cols})'
            )
            applied.append(f"{table}:{name} (unique index added)")
    return applied


def migrate(engine: Engine) -> list[str]:
    """迁移总入口：改名 → 补列 → 补唯一索引。顺序不能反（先补列，索引才建得起来）。"""
    return (
        migrate_renames(engine)
        + migrate_add_columns(engine)
        + migrate_add_unique_indexes(engine)
    )


def init_db(url: Optional[str] = None, *, echo: bool = False, create: bool = True) -> Engine:
    """建立（或重建）全局 engine + SessionLocal，并按需建表 + 跑迁移。"""
    global _engine, _SessionLocal, MIGRATIONS_APPLIED

    resolved = resolve_db_url(url)
    connect_args = {"check_same_thread": False} if resolved.startswith("sqlite") else {}
    engine = create_engine(resolved, echo=echo, future=True, connect_args=connect_args)
    if resolved.startswith("sqlite"):
        event.listen(engine, "connect", _on_connect)

    _engine = engine
    _SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    if create:
        # 顺序要紧：**先迁移、后建表**。
        # 反过来的话，老库上 create_all 会看到表已存在而跳过，
        # 新加的列永远补不上。
        #
        # 用 clear+extend 就地改这个列表，**不要** `MIGRATIONS_APPLIED = ...` 重新绑定：
        # 重新绑定会让 `from server.db import MIGRATIONS_APPLIED` 的引用永远指向旧对象，
        # 外部读到的永远是空列表（启动日志就会漏掉迁移记录）。
        MIGRATIONS_APPLIED.clear()
        MIGRATIONS_APPLIED.extend(migrate(engine))
        Base.metadata.create_all(engine)
    return engine


def get_engine() -> Engine:
    if _engine is None:
        init_db()
    assert _engine is not None
    return _engine


def ensure_db(url: Optional[str] = None) -> Engine:
    """启动钩子用：已经绑好库就直接复用。

    这条很重要 —— 否则测试里先 init_db(临时库)，应用启动钩子又会把它重置回
    默认的 data/checkup.db，测试就跑到真库上去了。
    """
    if _engine is not None and url is None:
        return _engine
    return init_db(url)


def is_initialized() -> bool:
    return _engine is not None


def get_session() -> Session:
    if _SessionLocal is None:
        init_db()
    assert _SessionLocal is not None
    return _SessionLocal()


@contextmanager
def session_scope() -> Iterator[Session]:
    """脚本/种子数据用：自动 commit / rollback / close。"""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖注入用。事务边界交给路由显式 commit。"""
    session = get_session()
    try:
        yield session
    finally:
        session.close()
