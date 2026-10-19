"""FastAPI 入口（施工单 3.2 / server/main.py）。

除挂载三个业务路由外，这里额外承担两个**系统级只读端点**：

    GET /api/health        健康检查（含 LLM 抽象层状态，不回显 key）
    GET /api/meta/fields   字段注册表（前端表单由此渲染 —— 加字段不用改前端）
    GET /api/meta/metrics  指标注册表

为什么放在 main.py 而不是新开 routers/meta.py：
    施工单 3.2 的目录结构没有 meta 路由文件。加文件 = 偏离施工单，
    而把两个只读的系统端点放应用入口是常见的收敛做法。见交付报告「偏差」一节。

M6-4（docs/06 §5.3）：`/api/health` 加了**数据库可写**检查。
原来只查"能连上"，但"能读不能写"（磁盘满、权限、只读挂载）时照样返回 ok ——
监控看到绿屏，实际老板的录入全在失败。现在用 `BEGIN IMMEDIATE` 抢一次写锁再回滚，
不写任何数据、不新增表，就能证明"数据库真的能写"。

前端构建产物挂载：若 web/dist 存在，直接挂在 /（方便只用后端端口验证）。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import __version__
from .access import (
    AccessKeyMiddleware,
    build_cors_kwargs,
    is_public_mode,
    validate_public_config,
)
from .db import ensure_db, get_db, session_scope
from .llm import get_provider
from .middleware import ErrorLogMiddleware, RateLimitMiddleware
from .models import FieldDef, MetricDef, Shop
from .routers import bom, daily, data_rights, portfolio, report, shops, stock, supplier
from .schemas import FieldOut, MetricOut
from .seed_metrics import seed_all

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIST = PROJECT_ROOT / "web" / "dist"

_TRUTHY = {"1", "true", "True", "yes", "on"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # M7 WP-1（docs/08 §4.2）：公网模式配置不合法 → **拒绝启动**。
    # 宁可不启动也不能"看起来安全其实全裸"：访问码没配/太短就暴露公网，
    # 老板以为有保护，实际任何人都能读营业额 —— 比明确拒绝更糟。
    validate_public_config()

    # 注意：用 ensure_db 而不是 init_db —— 测试里已绑好临时库时不能被重置回默认库
    ensure_db()
    if str(os.environ.get("SEED_ON_STARTUP", "1")).strip() in _TRUTHY:
        with session_scope() as session:
            seed_all(session)
    yield


app = FastAPI(
    title="单店经营体检 API",
    version=__version__,
    description=(
        "给没有电脑的餐饮小店老板：手机 10 秒录入，当天看到钱花在哪、哪个环节在漏。\n\n"
        "M1 范围：单门店 / 6 个原子数字 / 指标注册表驱动的 5 个指标 / 三行大白话结论。"
    ),
    lifespan=lifespan,
)

# CORS 参数由纯函数 build_cors_kwargs() 决定（M7 WP-1，docs/08 §4.2）：
#   公网模式 → 只放行 ALLOWED_ORIGINS 列出的来源（留空 = 不放行任何跨域，最安全）；
#   非公网   → 保持 M1 以来现状 ["*"]（Vite 开发服务器跨端口调用需要）。
# 抽成纯函数是因为 add_middleware 在 import 时执行，环境变量此刻定格，
# 测试没法启动后再改 —— 给函数喂一个 env 字典就能断言。
app.add_middleware(CORSMiddleware, **build_cors_kwargs())

# M7 WP-1：访问码闸门。中间件顺序依据（Starlette：**后 add 的在外层**）：
#   add 顺序 = CORS → AccessKey → RateLimit → ErrorLog
#   层序     = ErrorLog(最外) → RateLimit → AccessKey → CORS(最内)
#   * ErrorLog 必须能"看见"内层产生的 401/429 并记下来 —— 它在外层，
#     通过 call_next 拿到最终响应再按 status_code>=400 记录，所以没问题。
#   * AccessKey 在 CORS 外层：401 响应也会被 CORS 补上放行头（同源部署用不到，
#     但局域网 Vite 开发时前端才能读到 401 状态码而不是网络错误）。
#   * AccessKey 在 RateLimit 内层：恶意请求先被访问码挡掉，不计入单店限流。
app.add_middleware(AccessKeyMiddleware)

# M6-4（docs/06 §5.2 / §5.5）：API 错误日志 + 单店写限流。
app.add_middleware(ErrorLogMiddleware)
app.add_middleware(RateLimitMiddleware)

app.include_router(shops.router)
app.include_router(daily.router)
app.include_router(report.router)
# M3-3：库存与账实差异（goods / stocktakes / scraps 三张新表的入口）
app.include_router(stock.router)
# M4-6/7：多店归属、汇总与对比
app.include_router(portfolio.router)
# M5-0：成本卡与量价差异（账面公式里的"领用"从这里来）
app.include_router(bom.router)
# M5-1：供应商与应付账款
app.include_router(supplier.router)
# M6-2：数据归属（导出 Excel / 完整删除 / 隐私声明）
app.include_router(data_rights.router)


# --------------------------------------------------------------------------- #
# 系统端点
# --------------------------------------------------------------------------- #
@app.get("/api/health", tags=["system"], summary="健康检查")
def health(session: Session = Depends(get_db)) -> dict:
    # M7 WP-1（docs/08 §4.2）：公网模式下**只回探活所需的最小字段**。
    # llm 配置、字段/指标数量、门店数量对探活毫无用处，却是给攻击者的侦察情报
    # （比如"shops: 3"告诉对方这是多店部署）。非公网模式保持 M6 以来的完整返回。
    if is_public_mode():
        db_writable = _db_writable(session)
        return {
            "status": "ok" if db_writable else "degraded",
            "version": __version__,
            "db_writable": db_writable,
        }

    fields = len(session.scalars(select(FieldDef).where(FieldDef.enabled == 1)).all())
    metrics = len(session.scalars(select(MetricDef).where(MetricDef.enabled == 1)).all())

    db_writable, db_error = _db_writable(session, with_error=True)

    return {
        "status": "ok" if db_writable else "degraded",
        "version": __version__,
        "enabled_fields": fields,
        "enabled_metrics": metrics,
        "shops": len(session.scalars(select(Shop.id)).all()),
        "db_writable": db_writable,
        "db_error": db_error,
        "llm": get_provider().describe(),
    }


def _db_writable(session: Session, with_error: bool = False):
    """M6-4（docs/06 §5.3）：**可写**检查，不只是"能连上"。

    用 BEGIN IMMEDIATE 抢一次写锁再回滚 —— 不写数据、不建表，
    但能证明磁盘没满、文件没被挂成只读、目录可写。
    "能读不能写"时原来的实现照样返回 ok，监控一片绿而老板的录入全在失败。
    """
    db_writable = False
    db_error: str | None = None
    try:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        session.connection().exec_driver_sql("ROLLBACK")
        db_writable = True
    except Exception as exc:                     # noqa: BLE001 - 健康检查要把任何错都变短句
        db_error = str(exc).split("\n")[0][:200]
    if with_error:
        return db_writable, db_error
    return db_writable


@app.get("/api/meta/fields", response_model=list[FieldOut], tags=["meta"], summary="字段注册表")
def meta_fields(
    scope: str | None = None,
    session: Session = Depends(get_db),
) -> list[FieldDef]:
    stmt = select(FieldDef).where(FieldDef.enabled == 1).order_by(FieldDef.sort_order, FieldDef.code)
    if scope:
        stmt = stmt.where(FieldDef.scope == scope)
    return list(session.scalars(stmt).all())


@app.get("/api/meta/metrics", response_model=list[MetricOut], tags=["meta"], summary="指标注册表")
def meta_metrics(session: Session = Depends(get_db)) -> list[MetricDef]:
    return list(session.scalars(select(MetricDef).order_by(MetricDef.code)).all())


# --------------------------------------------------------------------------- #
# 前端静态产物（有就挂，没有不影响 API）
# --------------------------------------------------------------------------- #
if WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="web")
