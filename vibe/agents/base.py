from __future__ import annotations

import os
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
            if (
                (not mock_mode_enabled())
                and bool(getattr(self.config, "web_search", False))
                and str(getattr(self.config, "provider", "") or "").strip().lower() == "dashscope"
            ):
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
