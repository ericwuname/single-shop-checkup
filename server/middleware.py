"""M6-4 稳定性中间件：API 错误日志 + 单店限流（docs/06 §5.2 / §5.5）。

## 两条各自的理由

**错误日志**：出问题时（老板说"我保存失败了"）要能回溯是哪一次请求、什么错。
但**绝不记业务数据内容** —— 营业额、采购额是老板的商业秘密，
日志里出现这些数字，等于把账本摊在日志文件里。所以只记：时间、方法、路径、
状态码、店 id、异常类型与消息。

**限流**：防"误操作打爆"。最常见的场景不是攻击，是老板手机上的页面卡了，
他连点十几次保存 —— 于是同一条数据被写十遍。限流是**保护数据质量**，
顺带也防恶意刷。判据是**单店每分钟请求数**（不是单 IP，
M0 阶段大家基本都在同一个局域网/NAT 后面，按 IP 限会误伤）。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict, deque
from typing import Deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("checkup.api")

#: 单店每分钟请求上限。默认 120：一个店晚上 10 点集中录数，
#: 一分钟 120 次请求已经是很大的量（正常录入一次 3~5 个请求）。
#: 测试里会被调大（见 conftest），否则一次跑几百个请求全被限住。
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "120"))

#: 限流只盯**写操作**。读（看报告）打不坏数据库，限它只会让老板打不开页面。
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

#: 不参与限流的路径（健康检查被监控每秒打一次，限它就废了）
_EXEMPT_PREFIXES = ("/api/health", "/api/meta", "/docs", "/openapi.json")


class ErrorLogMiddleware(BaseHTTPMiddleware):
    """记录每次 API 错误（4xx/5xx）。**不记请求体** —— 那是老板的商业秘密。"""

    async def dispatch(self, request: Request, call_next):
        started = time.perf_counter()
        # 先把 body 读一次并缓存：限流中间件要按 shop_id 判，
        # 错误日志也要按 shop_id 回溯。读一次、后面复用，不会把 body 读空。
        shop_id = await _extract_shop_id(request)

        try:
            response = await call_next(request)
        except Exception:
            # 未捕获异常：middleware 兜底记一条，再抛给上层（否则 500 会静默）
            logger.exception(
                "未捕获异常 %s %s shop=%s",
                request.method,
                request.url.path,
                shop_id,
            )
            raise

        if response.status_code >= 400:
            ms = (time.perf_counter() - started) * 1000
            logger.warning(
                "API 错误 %s %s -> %s (%.0fms) shop=%s",
                request.method,
                request.url.path,
                response.status_code,
                ms,
                shop_id,
            )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """单店每分钟写请求上限。超限返回 429 + 一句大白话。"""

    def __init__(self, app, limit: int | None = None) -> None:
        super().__init__(app)
        self.limit = limit or RATE_LIMIT_PER_MINUTE
        self._hits: dict[int, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    async def dispatch(self, request: Request, call_next):
        if request.method not in _WRITE_METHODS or _is_exempt(request.url.path):
            return await call_next(request)

        shop_id = await _extract_shop_id(request)
        if shop_id is None:
            # 取不到店 id 的写请求不限流（系统类），但也不计账
            return await call_next(request)

        now = time.monotonic()
        with self._lock:
            bucket = self._hits[shop_id]
            while bucket and now - bucket[0] > 60.0:
                bucket.popleft()
            if len(bucket) >= self.limit:
                retry_after = int(60 - (now - bucket[0])) + 1
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": (
                            f"这一分钟提交了太多次（上限 {self.limit} 次）。"
                            f"等 {retry_after} 秒再试 —— "
                            f"如果页面卡住了，刷新比反复点保存更管用。"
                        )
                    },
                    headers={"Retry-After": str(retry_after)},
                )
            bucket.append(now)

        return await call_next(request)


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _is_exempt(path: str) -> bool:
    return any(path.startswith(p) for p in _EXEMPT_PREFIXES)


async def _extract_shop_id(request: Request) -> int | None:
    """从 query 或 JSON body 里取 shop_id。取不到返回 None（不猜）。

    ⚠️ 读 body **必须缓存**：`await request.body()` 会消费流，
    不缓存的话后面的路由读 body 只能读到空 —— 那比限流失败严重得多。
    Starlette 的 `Request` 会把 body 缓存在 `_body` 上，`json()` 复用它，
    所以这里读一次、路由里再读一次是安全的。
    """
    raw = request.query_params.get("shop_id")
    if raw and raw.isdigit():
        return int(raw)

    cached = getattr(request.state, "_shop_id_cache", None)
    if cached is not None:
        return cached or None
    try:
        body = await request.body()
        if not body:
            return None
        import json

        data = json.loads(body)
        if isinstance(data, dict) and isinstance(data.get("shop_id"), int):
            request.state._shop_id_cache = data["shop_id"]
            return data["shop_id"]
        request.state._shop_id_cache = 0
    except Exception:                        # noqa: BLE001 - 解析失败就当取不到
        return None
    return None
