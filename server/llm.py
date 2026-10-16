"""LLM 抽象层（施工单 2.3，工程硬约束）。

**业务代码里禁止出现任何厂商 SDK。** 换模型 = 改配置，不改业务代码。

M1 的边界（施工单 3.2）：
    只留接口，不接真模型。``LLM_ENABLED=0``（默认）时，``chat`` / ``vision``
    直接抛 ``LLMNotConfiguredError``，不发起任何网络请求。

为什么现在就要写这个文件而不是 M3 再写：
  ① 施工单 2.3 明确要求 M1 就留接口；
  ② 有接口才能写"不得绕过抽象层"的单测（tests/test_llm.py），
     否则 M3 接模型时会有人直接在路由里 ``import zhipuai``，半年后换模型就要翻全库。

诚实声明：``chat`` / ``vision`` 的**传输实现刻意留空**。本文件不做任何假装能跑的事。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_CHAT_MODEL = "glm-4.6"      # 施工单 2.1
DEFAULT_VISION_MODEL = "glm-4v"     # 施工单 2.2（M3 启用）


class LLMError(RuntimeError):
    """LLM 层异常基类。"""


class LLMNotConfiguredError(LLMError):
    """M1 正常状态：接口在，模型没接。"""


class LLMDisabledError(LLMError):
    """显式关闭。"""


@dataclass
class LLMProvider:
    """统一的模型入口。所有调用点只认这一层。"""

    provider: str = "zhipu"
    model: str = DEFAULT_CHAT_MODEL
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    vision_model: str = DEFAULT_VISION_MODEL
    timeout_seconds: float = 30.0
    enabled: bool = False
    #: 传输后端留空 —— M3 填。填的是「怎么发 HTTP」，不是「业务怎么用模型」。
    transport: Any = field(default=None, repr=False)

    # ---- 构造 ------------------------------------------------------------ #
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LLMProvider":
        env = env if env is not None else os.environ
        return cls(
            provider=env.get("LLM_PROVIDER", "zhipu"),
            model=env.get("LLM_MODEL", DEFAULT_CHAT_MODEL),
            api_key=env.get("LLM_API_KEY", ""),
            base_url=env.get("LLM_BASE_URL", DEFAULT_BASE_URL),
            vision_model=env.get("LLM_VISION_MODEL", DEFAULT_VISION_MODEL),
            timeout_seconds=float(env.get("LLM_TIMEOUT_SECONDS", "30") or 30),
            enabled=str(env.get("LLM_ENABLED", "0")).strip() in {"1", "true", "True", "yes"},
        )

    @classmethod
    def from_dotenv(cls, path: str | Path | None = None) -> "LLMProvider":
        """读 .env（可选依赖 python-dotenv；没装就退回纯环境变量）。"""
        env: dict[str, str] = dict(os.environ)
        candidate = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"
        if candidate.exists():
            try:
                from dotenv import dotenv_values  # type: ignore

                env.update({k: v for k, v in dotenv_values(candidate).items() if v is not None})
            except ImportError:  # pragma: no cover
                pass
        return cls.from_env(env)

    # ---- 状态 ------------------------------------------------------------ #
    @property
    def is_enabled(self) -> bool:
        return bool(self.enabled and self.api_key and self.transport is not None)

    def describe(self) -> dict[str, Any]:
        """给 /api/health 用。**不回显 api_key**。"""
        return {
            "provider": self.provider,
            "model": self.model,
            "vision_model": self.vision_model,
            "base_url": self.base_url,
            "enabled": self.is_enabled,
            "api_key_set": bool(self.api_key),
            "transport": type(self.transport).__name__ if self.transport else None,
        }

    # ---- 接口 ------------------------------------------------------------ #
    def chat(self, messages: Sequence[Mapping[str, str]], **kwargs: Any) -> str:
        """文本对话。消息形如 ``[{"role": "user", "content": "..."}]``。"""
        self._require_ready("chat")
        raise LLMNotConfiguredError(
            "M1 不接真模型：chat 的传输实现刻意留空（施工单 3.2）。"
            "M3 接入时只需给 self.transport 一个实现，业务代码不用动。"
        )

    def vision(self, image_path: str | Path, prompt: str, **kwargs: Any) -> dict[str, Any]:
        """图片理解/OCR（M3 拍小票、拍排班纸）。约定返回结构化 dict。"""
        self._require_ready("vision")
        raise LLMNotConfiguredError(
            "M1 不接真模型：vision 的传输实现刻意留空（施工单 3.2）。"
            f"M3 用 {self.vision_model}（GLM-4V）填传输层。"
        )

    # ---- 内部 ------------------------------------------------------------ #
    def _require_ready(self, action: str) -> None:
        if not self.enabled:
            raise LLMNotConfiguredError(
                f"LLM 未启用（LLM_ENABLED=0），无法执行 {action}。这是 M1 的预期状态。"
            )
        if not self.api_key:
            raise LLMNotConfiguredError(f"LLM 未配置 API Key，无法执行 {action}。")
        if self.transport is None:
            raise LLMNotConfiguredError(
                f"{action} 的传输后端未注入（transport=None）。M1 不提供默认传输。"
            )


_default_provider: LLMProvider | None = None


def get_provider(refresh: bool = False) -> LLMProvider:
    """全局单例。业务代码拿模型的唯一入口。"""
    global _default_provider
    if _default_provider is None or refresh:
        _default_provider = LLMProvider.from_dotenv()
    return _default_provider
