from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Type, TypeVar

from pydantic import BaseModel

from vibe.config import AgentConfig, ProviderConfig
from vibe.providers.base import ProviderError, ProviderResult
from vibe.providers.dashscope import DashScopeProvider
from vibe.providers.deepseek import DeepSeekProvider
from vibe.providers.mock import MockProvider


T = TypeVar("T", bound=BaseModel)


def mock_mode_enabled() -> bool:
    return os.getenv("VIBE_MOCK_MODE", "").strip() == "1"


class BaseAgent:
    agent_id: str
    output_schema: Type[BaseModel]
    default_provider: str
    default_model: str

    def __init__(self, config: AgentConfig, *, providers: Dict[str, ProviderConfig]) -> None:
        self.config = config
        self.providers = providers
        self.provider = self._make_provider(config.provider)

    def _make_provider(self, provider_id: str):
        if mock_mode_enabled() or provider_id == "mock":
            return MockProvider()
        prov = self.providers.get(provider_id)
        if not prov:
            raise ProviderError(f"Provider not configured: {provider_id}")
        if provider_id == "deepseek":
            return DeepSeekProvider(base_url=prov.base_url, api_key_env=prov.api_key_env or "DEEPSEEK_API_KEY")
        if provider_id == "dashscope":
            return DashScopeProvider(base_url=prov.base_url, api_key_env=prov.api_key_env or "DASHSCOPE_API_KEY")
        raise ProviderError(f"Unknown provider: {provider_id}")

    def chat_json(
        self,
        *,
        schema: Type[T],
        user: str,
        system: Optional[str] = None,
        messages: Optional[List[Dict[str, str]]] = None,
        temperature: float = 0.0,
    ) -> tuple[T, ProviderResult]:
        if messages is None:
            msgs: List[Dict[str, str]] = []
            if system:
                msgs.append({"role": "system", "content": system})
            msgs.append({"role": "user", "content": user})
        else:
            msgs = messages

        extra_body: Optional[Dict[str, Any]] = None
        try:
            provider_id = str(getattr(self.config, "provider", "") or "").strip().lower()
            if (not mock_mode_enabled()) and provider_id == "dashscope":
                mode = getattr(self.config, "web_search", "off")
                if mode is True:
                    mode = "on"
                elif mode is False or mode is None:
                    mode = "off"
                mode = str(mode).strip().lower()

                if mode == "on":
                    extra_body = {"enable_search": True}
                elif mode == "auto":
                    if self._should_enable_dashscope_search(messages=msgs, schema=schema):
                        extra_body = {"enable_search": True}
        except Exception:
            extra_body = None

        kwargs: Dict[str, Any] = {
            "model": self.config.model,
            "messages": msgs,
            "schema": schema,
            "temperature": temperature,
        }
        if extra_body is not None:
            kwargs["extra_body"] = extra_body

        return self.provider.chat_json(**kwargs)

    def _should_enable_dashscope_search(self, *, messages: List[Dict[str, str]], schema: Type[BaseModel]) -> bool:
        """
        Heuristic, best-effort:
        - Only enable search for "fact verification" situations.
        - Avoid turning search on during code generation/debug loops.
        """
        try:
            agent_id = str(getattr(self.config, "id", "") or "").strip().lower()
            if agent_id.startswith("coder_") or agent_id in {"qa", "integration_engineer"}:
                return False
        except Exception:
            pass

        schema_name = getattr(schema, "__name__", "") or ""
        if schema_name in {"ContractPack", "ReferenceItem", "RiskRegister"}:
            return True

        text = "\n".join([str(m.get("content") or "") for m in (messages or [])])
        low = text.lower()

        # Require a "verification intent" signal so we don't enable search on generic planning.
        intent_tokens = [
            "verify",
            "confirm",
            "documentation",
            "docs",
            "is this correct",
            "latest",
            "source",
            "citation",
            "查证",
            "确认",
            "是否正确",
            "最新",
            "文档",
            "官网",
            "来源",
            "引用",
        ]
        if not any(t in low for t in intent_tokens):
            return False

        # Now look for cues that external facts are involved.
        cue_re = re.compile(
            r"(?i)\b(api|endpoint|base[_-]?url|openai[- ]?compatible|model(?:\s+name)?|dashscope|deepseek|sdk|spec)\b|接口|端点|模型|版本|协议|鉴权|错误码"
        )
        return bool(cue_re.search(text or ""))
