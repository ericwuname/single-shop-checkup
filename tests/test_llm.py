"""LLM 抽象层守卫（施工单 2.3「工程硬约束」）。

这个文件存在的理由：约束写在文档里没人守，写在测试里才守得住。
最后一条 test_no_vendor_sdk_in_business_code 是**静态扫描**——
它检查 server/ 下任何业务文件都没直接 import 厂商 SDK。
M3 接模型时若有人图省事在路由里 `import zhipuai`，这条会红。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from server.llm import LLMNotConfiguredError, LLMProvider, get_provider

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"

VENDOR_SDK_PATTERNS = (
    "zhipuai",
    "openai",
    "dashscope",
    "anthropic",
    "google.generativeai",
    "volcengine",
    "ark",
    "requests",
    "httpx",
)


def test_disabled_by_default_and_raises_clear_error():
    provider = LLMProvider()
    assert provider.is_enabled is False
    with pytest.raises(LLMNotConfiguredError):
        provider.chat([{"role": "user", "content": "你好"}])
    with pytest.raises(LLMNotConfiguredError):
        provider.vision("uploads/x.jpg", "识别这张小票")


def test_from_env_reads_zhipu_defaults_per_spec():
    provider = LLMProvider.from_env(
        {
            "LLM_PROVIDER": "zhipu",
            "LLM_MODEL": "glm-4.6",
            "LLM_API_KEY": "test-key",
            "LLM_ENABLED": "0",
        }
    )
    assert provider.model == "glm-4.6"          # 施工单 2.1
    assert provider.base_url.endswith("/api/paas/v4")
    assert provider.vision_model == "glm-4v"    # 施工单 2.2
    assert provider.is_enabled is False         # 没给 transport 就是没接


def test_enabled_without_transport_still_refuses():
    """开了开关但没注入传输层 —— 仍然不许静默假装成功。"""
    provider = LLMProvider(api_key="test-key", enabled=True)
    assert provider.is_enabled is False
    with pytest.raises(LLMNotConfiguredError):
        provider.chat([])


def test_describe_never_leaks_api_key():
    provider = LLMProvider(api_key="sk-super-secret", enabled=True)
    described = provider.describe()
    assert "sk-super-secret" not in str(described)
    assert described["api_key_set"] is True


def test_get_provider_is_singleton_and_refreshable():
    a = get_provider(refresh=True)
    b = get_provider()
    assert a is b


def test_no_vendor_sdk_in_business_code():
    """业务代码里不许出现厂商 SDK / 裸 HTTP 客户端 —— 只能走 llm.LLMProvider。"""
    offenders: list[str] = []
    for path in sorted(SERVER_DIR.rglob("*.py")):
        if path.name == "llm.py":
            continue  # 抽象层自己将来会 import 传输实现，允许
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                root = module.split(".")[0]
                if root in VENDOR_SDK_PATTERNS:
                    offenders.append(f"{path.name}: import {module}")
    assert not offenders, f"业务代码里出现了厂商 SDK/裸 HTTP 客户端：{offenders}"


def test_no_hardcoded_api_key_in_repo():
    """密钥不许硬编码在源码里（.env.example 只留占位）。"""
    pattern = re.compile(r"(sk-[A-Za-z0-9]{16,}|[0-9a-f]{32}\.[A-Za-z0-9]{16,})")
    offenders: list[str] = []
    for path in sorted(Path(__file__).resolve().parent.parent.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".js", ".vue", ".json", ".md", ".txt", ".example"}:
            continue
        if "node_modules" in path.parts or path.name.startswith("."):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in pattern.finditer(text):
            offenders.append(f"{path.name}: {match.group(0)[:12]}…")
    assert not offenders, f"源码里出现疑似硬编码密钥：{offenders}"
