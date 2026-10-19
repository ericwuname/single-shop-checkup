"""公网访问控制（M7 WP-1，docs/08 §4）。

## 设计（评审窗口已定，不擅自改方案）

**单租户访问码**：一个部署 = 一个访问码 = 一位老板。
不建用户表、不做账号体系（多老板是 docs/09 的 M10，M0 过关前不碰）。

| 环境变量 | 含义 |
|---|---|
| `PUBLIC_MODE` | 真值（1/true/yes/on）= 公网模式；默认关 |
| `APP_ACCESS_KEY` | 访问码。公网模式下**必须 ≥ 20 位**，否则服务拒绝启动 |
| `ALLOWED_ORIGINS` | 逗号分隔的 CORS 白名单；公网模式留空 = 不发任何放行头 |

## 为什么公网前必须有它（评审窗口的实测，我复现确认过）

M6 之前后端零访问控制：`GET /api/shops` 谁都能读（列出**全部门店**）、
`POST /api/shops` 谁都能建店、CORS 是 `allow_origins=["*"]`。
本机起服务后 `curl /api/shops` 直接返回全部门店 ——
一旦用 Cloudflare Tunnel 暴露到公网，任何知道地址的人都能读写营业额。
所以 docs/08 §0 最重要的一条：**WP-1 验收通过之前，任何服务不许暴露公网**。

## 实现要点（逐条对应任务书 §4.2 的 dispatch 规则）

1. 非公网模式 → 一切照旧（旧行为逐字节不变，全部旧测试不受影响）
2. `OPTIONS`（CORS 预检）→ 放行（预检不该要访问码，否则浏览器跨域全挂）
3. `/docs` `/redoc` `/openapi.json` → **404 而不是 401**（不暴露"这里有文档"）
4. 非 `/api/` 路径（前端静态文件、manifest、图标）→ 放行（访问码在页面里输）
5. `GET /api/health` → 放行（监控探活）
6. 其余 `/api/*` → 读 `X-Access-Key` 头，`hmac.compare_digest` **恒定时间比较**
   （用 `==` 会被时序攻击逐字节猜出来，任务书明令禁止）
7. 防暴力：按 IP 记失败数，10 分钟内 ≥10 次失败 → 429，
   封锁期内**对码也 429**（否则封锁毫无意义）。内存字典，不落库。
8. 访问码绝不出现在日志/异常/响应体/URL —— 只走请求头
"""

from __future__ import annotations

import hmac
import os
import threading
import time
from collections import defaultdict, deque
from typing import Deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

_TRUTHY = {"1", "true", "True", "yes", "on"}

#: 公网模式下访问码的最小长度
MIN_KEY_LENGTH = 20

#: 防暴力窗口与阈值：10 分钟内失败 ≥ 10 次 → 封 10 分钟
FAIL_WINDOW_SECONDS = 600.0
FAIL_THRESHOLD = 10

#: 访问码永远不该出现的响应头/字段
_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


def is_public_mode() -> bool:
    """是否公网模式。**每次调用都读环境变量**，不在 import 时缓存 ——
    测试要用 monkeypatch 切换，缓存了就切不动了。"""
    return str(os.environ.get("PUBLIC_MODE", "")).strip().lower() in _TRUTHY


def validate_public_config() -> None:
    """公网模式下配置不合法 → 抛 RuntimeError（中文原因），**服务拒绝启动**。

    宁可不启动也不能"看起来安全其实全裸"：服务起了、访问码没配，
    老板以为有保护，实际任何人都能读营业额 —— 那比明确拒绝更糟。
    """
    if not is_public_mode():
        return
    key = (os.environ.get("APP_ACCESS_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "公网模式（PUBLIC_MODE=1）必须设置 APP_ACCESS_KEY（访问码）。"
            "生成命令：python -c \"import secrets; print(secrets.token_urlsafe(24))\""
        )
    if len(key) < MIN_KEY_LENGTH:
        raise RuntimeError(
            f"APP_ACCESS_KEY 太短（{len(key)} 位）。公网模式下至少 {MIN_KEY_LENGTH} 位，"
            "否则会被暴力猜出来。生成命令："
            "python -c \"import secrets; print(secrets.token_urlsafe(24))\""
        )


def parse_allowed_origins() -> list[str]:
    """解析 ALLOWED_ORIGINS（逗号分隔）。空白项剔除。"""
    raw = os.environ.get("ALLOWED_ORIGINS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def build_cors_kwargs(env: dict[str, str] | None = None) -> dict:
    """构造 CORSMiddleware 的参数（M7 WP-1，docs/08 §4.2）。

    抽成**纯函数**是为了可测：add_middleware 在 import 时执行，
    环境变量要在那一刻定格，测试没法在启动后再改。给它喂一个 env 字典即可断言。

    规则：
    * 公网模式 → 白名单取 ALLOWED_ORIGINS（可为空列表 = 不放行任何跨域，最安全）
    * 非公网模式 → 保持现状 `["*"]`（本机/Vite 开发场景，M1 以来一直这样）
    """
    env = dict(os.environ if env is None else env)
    public = str(env.get("PUBLIC_MODE", "")).strip().lower() in _TRUTHY
    if not public:
        return {
            "allow_origins": ["*"],
            "allow_credentials": False,
            "allow_methods": ["*"],
            "allow_headers": ["*"],
        }
    origins: list[str] = []
    for item in env.get("ALLOWED_ORIGINS", "").split(","):
        if item.strip():
            origins.append(item.strip())
    return {
        "allow_origins": origins,
        "allow_credentials": False,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    }


class AccessKeyMiddleware(BaseHTTPMiddleware):
    """公网模式下的访问码闸门（非公网模式零行为变化）。"""

    def __init__(self, app) -> None:
        super().__init__(app)
        self._failures: dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def reset_failure_tracking(self) -> None:
        """清空防暴力计数（**仅供测试**：用例之间隔离，否则失败次数会跨用例累计）。"""
        with self._lock:
            self._failures.clear()

    # ------------------------------------------------------------------ #
    async def dispatch(self, request: Request, call_next):
        # 规则 1：非公网模式 → 一切照旧。这是"旧行为不变"承诺的落点。
        if not is_public_mode():
            return await call_next(request)

        # 规则 2：CORS 预检放行（预检不带自定义头，拦了浏览器跨域就全挂）
        if request.method == "OPTIONS":
            return await call_next(request)

        # 规则 3：接口文档不给看，也不暴露存在（404 而不是 401）
        if request.url.path in _DOC_PATHS:
            return JSONResponse(status_code=404, content={"detail": "Not Found"})

        # 规则 4：非 /api/ 的（静态页面 / manifest / 图标）放行 —— 访问码在页面里输
        if not request.url.path.startswith("/api/"):
            return await call_next(request)

        # 规则 5：健康检查放行（外拨监控/UptimeRobot 没有访问码）
        if request.method == "GET" and request.url.path == "/api/health":
            return await call_next(request)

        # 防暴力检查放在验证之前：封锁期内对码也 429（否则封锁无意义）
        client_ip = self._client_ip(request)
        if self._is_blocked(client_ip):
            return JSONResponse(
                status_code=429,
                content={"detail": "尝试太多次，请 10 分钟后再试"},
            )

        # 规则 6：恒定时间比较。`==` 会在第一个不匹配字节提前返回，
        # 时序差可以被测量出来逐字节猜码 —— 任务书明令禁止。
        expected = (os.environ.get("APP_ACCESS_KEY") or "").strip()
        given = (request.headers.get("X-Access-Key") or "").strip()
        ok = hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))

        if not ok or not expected:
            # 注意：expected 为空在启动时就会被 validate_public_config 拦下，
            # 这里兜底按"没配 = 全拒"处理，绝不因为配置缺失而放行。
            self._record_failure(client_ip)
            # 规则 8：响应体只说"需要访问码"，绝不回显访问码本身或期望值
            return JSONResponse(status_code=401, content={"detail": "需要访问码"})

        # 规则 8：放行时同样不把访问码塞进任何响应头/日志 —— 只用不存
        return await call_next(request)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _client_ip(request: Request) -> str:
        """客户端 IP：有 CDN 头（Cloudflare Tunnel 场景）用之，否则用连接对端。

        ⚠️ 局限：直连模式下伪造 CF-Connecting-IP 可以绕过防暴力统计。
        这是**接受的取舍**：该头只有过 Cloudflare 才可信，而本中间件的
        主要威胁模型就是"通过 Cloudflare Tunnel 暴露"——那时该头由
        Cloudflare 设置、不可伪造。直连场景主要靠 RateLimit 兜底。
        """
        cf_ip = (request.headers.get("CF-Connecting-IP") or "").strip()
        if cf_ip:
            return cf_ip
        return request.client.host if request.client else "unknown"

    def _is_blocked(self, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._failures.get(ip)
            if not bucket:
                return False
            while bucket and now - bucket[0] > FAIL_WINDOW_SECONDS:
                bucket.popleft()
            return len(bucket) >= FAIL_THRESHOLD

    def _record_failure(self, ip: str) -> None:
        now = time.monotonic()
        with self._lock:
            bucket = self._failures[ip]
            bucket.append(now)
            while bucket and now - bucket[0] > FAIL_WINDOW_SECONDS:
                bucket.popleft()
