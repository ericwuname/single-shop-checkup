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

# 手机通过局域网 IP 访问 Vite 开发服务器的场景（M1 真机试用）需要宽松 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# M6-4（docs/06 §5.2 / §5.5）：API 错误日志 + 单店写限流。
# 顺序有讲究：错误日志要能记到限流产生的 429，所以它必须**包在外面**。
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
    fields = len(session.scalars(select(FieldDef).where(FieldDef.enabled == 1)).all())
    metrics = len(session.scalars(select(MetricDef).where(MetricDef.enabled == 1)).all())

    # M6-4（docs/06 §5.3）：**可写**检查，不只是"能连上"。
    # 用 BEGIN IMMEDIATE 抢一次写锁再回滚 —— 不写数据、不建表，
    # 但能证明磁盘没满、文件没被挂成只读、目录可写。
    # "能读不能写"时原来的实现照样返回 ok，监控一片绿而老板的录入全在失败。
    db_writable = False
    db_error: str | None = None
    try:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        session.connection().exec_driver_sql("ROLLBACK")
        db_writable = True
    except Exception as exc:                     # noqa: BLE001 - 健康检查要把任何错都变短句
        db_error = str(exc).split("\n")[0][:200]

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
