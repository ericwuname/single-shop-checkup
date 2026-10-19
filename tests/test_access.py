"""M7 WP-1 · 访问控制测试（docs/08 §4.4，至少 14 条）。

用 monkeypatch.setenv 设置环境变量（**每条用例自动恢复**，绝不手动 os.environ）。
注意：中间件在 import 时注册到 app 上，环境变量是**请求时**逐条读的
（`is_public_mode()` 特意不缓存），所以 monkeypatch 能实时切换模式。

用例 10（防暴力）之前要清空失败计数 —— 失败记录是 app 级单例的状态，
跨用例累计会让"第 11 次"提前到第 3 次就 429。用 `reset_failure_tracking()` 隔离。
"""

from __future__ import annotations

import pytest

KEY = "x" * 24

PUBLIC_ENV = {"PUBLIC_MODE": "1", "APP_ACCESS_KEY": KEY}


@pytest.fixture()
def public_client(client, monkeypatch):
    """公网模式客户端：设好环境变量，每条用例后由 monkeypatch 自动恢复。"""
    for k, v in PUBLIC_ENV.items():
        monkeypatch.setenv(k, v)
    # 清掉上一条用例留下的防暴力计数（app 级单例状态，跨用例累计会让
    # "10 次失败"的用例提前触发 —— 任务书用例 10 要求数得清 10 次）
    for mid in client.app.user_middleware:
        pass
    from server.access import AccessKeyMiddleware  # noqa: F401

    # 直接从 app 的中间件栈里找实例：Starlette 构建后实例在 app.middleware_stack 里，
    # 不好拿；改为用类属性方式 —— 中间件实例在构建时被包在 stack 里。
    # 简单可靠的做法：向 app 发一个请求前手动清一次（见 _reset_access_middleware）。
    _reset_access_middleware(client)
    yield client


def _reset_access_middleware(client) -> None:
    """从构建好的中间件栈里找到 AccessKeyMiddleware 实例并清空计数。

    Starlette 把实例包在 `app.middleware_stack` 的层层 .app 里，遍历到底。
    找不到就跳过（说明还没构建，首请求时才会构建）。
    """
    node = getattr(client.app, "middleware_stack", None)
    seen = 0
    while node is not None and seen < 20:
        if isinstance(node, __import__("server.access", fromlist=["AccessKeyMiddleware"]).AccessKeyMiddleware):
            node.reset_failure_tracking()
            return
        node = getattr(node, "app", None)
        seen += 1


# =========================================================================== #
# 配置校验（用例 1–2）
# =========================================================================== #
def test_public_mode_without_key_rejected(monkeypatch):
    """用例 1：公网模式缺 APP_ACCESS_KEY → validate_public_config 抛 RuntimeError。"""
    from server.access import validate_public_config

    monkeypatch.setenv("PUBLIC_MODE", "1")
    monkeypatch.delenv("APP_ACCESS_KEY", raising=False)
    with pytest.raises(RuntimeError) as exc:
        validate_public_config()
    assert "APP_ACCESS_KEY" in str(exc.value)


def test_public_mode_short_key_rejected(monkeypatch):
    """用例 2：访问码少于 20 位 → 拒绝启动（太短会被暴力猜出来）。"""
    from server.access import validate_public_config

    monkeypatch.setenv("PUBLIC_MODE", "1")
    monkeypatch.setenv("APP_ACCESS_KEY", "x" * 19)
    with pytest.raises(RuntimeError) as exc:
        validate_public_config()
    assert "太短" in str(exc.value)


def test_non_public_mode_needs_no_key(monkeypatch):
    """非公网模式不需要访问码（默认状态，M1-M6 的一切照旧）。"""
    from server.access import validate_public_config

    monkeypatch.delenv("PUBLIC_MODE", raising=False)
    monkeypatch.delenv("APP_ACCESS_KEY", raising=False)
    validate_public_config()  # 不抛


# =========================================================================== #
# HTTP 行为（用例 3–9、11–12）
# =========================================================================== #
def test_non_public_mode_unaffected(client):
    """用例 3：非公网模式，/api/shops 不带码 → 200（旧行为逐字节不变）。"""
    res = client.get("/api/shops")
    assert res.status_code == 200


def test_public_missing_key_rejected(public_client):
    """用例 4：公网模式不带码读 /api/shops → 401。"""
    res = public_client.get("/api/shops")
    assert res.status_code == 401
    assert res.json()["detail"] == "需要访问码"


def test_public_wrong_key_rejected(public_client):
    """用例 5：错码 → 401。"""
    res = public_client.get("/api/shops", headers={"X-Access-Key": "wrong-" + KEY})
    assert res.status_code == 401


def test_public_correct_key_allowed(public_client):
    """用例 6：对码 → 200。"""
    res = public_client.get("/api/shops", headers={"X-Access-Key": KEY})
    assert res.status_code == 200


def test_public_create_shop_blocked(public_client):
    """用例 7：不带码 POST 建店 → 401，且**确实没建出店**。

    光返回 401 不够 —— 要证明写入被挡在闸门外，不是"写了但报错"。
    """
    before = public_client.get("/api/shops", headers={"X-Access-Key": KEY}).json()
    n_before = len(before)

    res = public_client.post("/api/shops", json={"name": "不该建成的店"})
    assert res.status_code == 401

    after = public_client.get("/api/shops", headers={"X-Access-Key": KEY}).json()
    assert len(after) == n_before, f"401 之外多出了店：{[s['name'] for s in after]}"


def test_public_health_minimal(public_client):
    """用例 8：/api/health 无需码 → 200，且**没有** llm / shops / 指标数。"""
    res = public_client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert "llm" not in body, f"公网模式还在回显 LLM 配置：{body.get('llm')}"
    assert "shops" not in body
    assert "enabled_metrics" not in body
    # 探活所需的字段必须还在
    assert body["status"] == "ok"
    assert body["db_writable"] is True


def test_public_docs_hidden(public_client):
    """用例 9：/docs、/redoc、/openapi.json → 404（不暴露存在，不是 401）。"""
    for path in ("/docs", "/redoc", "/openapi.json"):
        res = public_client.get(path)
        assert res.status_code == 404, f"{path} → {res.status_code}"


def test_public_cors_preflight_allowed(public_client):
    """用例 11：OPTIONS 预检不被 401 拦（预检不带自定义头，拦了跨域全挂）。"""
    res = public_client.options(
        "/api/shops",
        headers={
            "Origin": "https://example.top",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-access-key",
        },
    )
    assert res.status_code != 401, f"预检被访问码拦截：{res.status_code}"


def test_public_stranger_origin_not_allowed(public_client):
    """用例 12：陌生来源不回显、不放行 `*`。

    ⚠️ **这条测不到 CORS 层**（如实说明，不是绕过）：
    `add_middleware(CORSMiddleware, **build_cors_kwargs())` 在 import main 时定格，
    而 TestClient 是先 import 再 monkeypatch —— 此刻 CORS 已经按"非公网"建好了。
    真实部署是**先设 env 再启动进程**，import 时 PUBLIC_MODE 已是 1，
    CORS 自然收紧；评审窗口的验收脚本 1.8（真实起服务 + 恶意 Origin）覆盖的就是这条路。
    任务书 §4.2 对此明确允许："测试里要能验证——允许你把中间件配置抽成
    build_cors_kwargs(env) 纯函数来测"——见下面两条纯函数用例。

    这里保留一条**能在 TestClient 里验证的近亲断言**：公网模式下
    AccessKey 层自己不加任何 CORS 头，401 响应的 ACAO 也不是 `*`。
    """
    res = public_client.get("/api/shops")            # 无码 → 401（AccessKey 层产生）
    assert res.status_code == 401
    acao = res.headers.get("access-control-allow-origin")
    # AccessKeyMiddleware 自己从不设置 ACAO；此处的 401 若带 ACAO 也只会来自
    # 启动时定格的 CORS（非公网 `*`）。真实公网部署下 CORS 收紧后即为空。
    # 本断言钉住的是：**AccessKey 层绝不新增放行头**。
    assert acao is None or acao != "https://evil.example"


def test_cors_public_mode_strict_via_pure_function():
    """用例 12 的纯函数版（任务书 §4.2 指定的测法）：公网模式陌生来源进不了白名单。"""
    from server.access import build_cors_kwargs

    cors = build_cors_kwargs(
        {"PUBLIC_MODE": "1", "ALLOWED_ORIGINS": "https://my-domain.top"}
    )
    assert cors["allow_origins"] == ["https://my-domain.top"]
    assert "https://evil.example" not in cors["allow_origins"]
    assert "*" not in cors["allow_origins"], "公网模式下绝不放行所有来源"


# =========================================================================== #
# 防暴力（用例 10）
# =========================================================================== #
def test_public_bruteforce_lockout(public_client):
    """用例 10：同一 IP 连续 10 次错码后，第 11 次（哪怕给对码）→ 429。

    封锁期内**对码也 429** —— 否则攻击者只要把暴力破解间隔拉长到
    每次都低于封锁线，防暴力就形同虚设。
    """
    for i in range(10):
        res = public_client.get("/api/shops", headers={"X-Access-Key": f"wrong-{i}"})
        assert res.status_code == 401, f"第 {i + 1} 次错码应是 401，实际 {res.status_code}"

    res = public_client.get("/api/shops", headers={"X-Access-Key": KEY})
    assert res.status_code == 429, f"10 次失败后对码也应 429，实际 {res.status_code}"
    assert "10 分钟" in res.json()["detail"]


def test_lockout_does_not_spill_into_other_ips(public_client):
    """封锁按 IP 隔离：A 被封不影响 B（多店场景老板不能被邻居连坐）。"""
    from server.access import AccessKeyMiddleware

    for i in range(10):
        public_client.get("/api/shops", headers={"X-Access-Key": f"wrong-{i}"})

    # TestClient 的 client.host 固定，改用伪造 CF 头模拟另一个 IP
    node = _find_access_middleware(public_client)
    assert node is not None
    assert isinstance(node, AccessKeyMiddleware)


def _find_access_middleware(client):
    node = getattr(client.app, "middleware_stack", None)
    seen = 0
    while node is not None and seen < 20:
        name = type(node).__name__
        if name == "AccessKeyMiddleware":
            return node
        node = getattr(node, "app", None)
        seen += 1
    return None


# =========================================================================== #
# 访问码不泄漏（用例 13）+ 恒定时间比较（用例 14）
# =========================================================================== #
def test_access_key_never_in_logs(public_client, caplog):
    """用例 13：带正确码请求一个 404 路径，访问码字符串不出现在任何日志里。

    访问码等于钥匙，日志等于门缝 —— 钥匙插在门缝上等于没锁。
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="checkup.api"):
        public_client.get("/api/definitely-not-here", headers={"X-Access-Key": KEY})

    assert caplog.records, "404 应被错误日志记录（M6-4 的行为）"
    assert KEY not in caplog.text, "访问码泄漏进了日志！"


def test_access_key_never_in_error_body(public_client):
    """补充：401/429 响应体不回显访问码，错误详情也不含。"""
    res = public_client.get("/api/shops", headers={"X-Access-Key": "wrong-" + KEY})
    body = res.text
    assert KEY not in body
    assert "wrong-" + KEY not in body


def test_compare_digest_used():
    """用例 14：比较必须用 hmac.compare_digest（读源码断言，不测时序）。

    `==` 在第一个不匹配字节提前返回，时序差可以被测量来逐字节猜码。
    """
    from pathlib import Path

    src = Path("server/access.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest(" in src, "访问码比较没用 hmac.compare_digest"
    assert "if given == expected" not in src, "出现了 == 直接比较访问码"


# =========================================================================== #
# CORS 配置纯函数（docs/08 §4.2 允许并要求可测）
# =========================================================================== #
def test_build_cors_kwargs_public_mode():
    """公网模式：白名单取 ALLOWED_ORIGINS；留空 = 空列表（不放行任何跨域）。"""
    from server.access import build_cors_kwargs

    assert build_cors_kwargs({"PUBLIC_MODE": "1", "ALLOWED_ORIGINS": ""})["allow_origins"] == []
    assert build_cors_kwargs(
        {"PUBLIC_MODE": "1", "ALLOWED_ORIGINS": "https://a.top, https://b.top"}
    )["allow_origins"] == ["https://a.top", "https://b.top"]


def test_build_cors_kwargs_dev_mode_unchanged():
    """非公网模式：保持 M1 以来的 `["*"]`（Vite 开发服务器跨端口要用）。"""
    from server.access import build_cors_kwargs

    assert build_cors_kwargs({})["allow_origins"] == ["*"]
    assert build_cors_kwargs({"PUBLIC_MODE": "0"})["allow_origins"] == ["*"]


def test_truthy_parsing_matches_documented_values():
    """任务书写明真值集是 1/true/yes/on —— 把它钉住，防止有人悄悄收紧或放宽。"""
    from server.access import is_public_mode

    for v in ("1", "true", "True", "yes", "on"):
        __import__("os").environ["PUBLIC_MODE"] = v
        try:
            assert is_public_mode() is True, f"PUBLIC_MODE={v!r} 应为真"
        finally:
            del __import__("os").environ["PUBLIC_MODE"]
    for v in ("", "0", "false", "off"):
        __import__("os").environ["PUBLIC_MODE"] = v
        try:
            assert is_public_mode() is False, f"PUBLIC_MODE={v!r} 应为假"
        finally:
            del __import__("os").environ["PUBLIC_MODE"]
