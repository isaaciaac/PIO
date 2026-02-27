from __future__ import annotations

import os
from typing import Dict, List, Optional, Type, TypeVar

from pydantic import BaseModel

from vibe.config import AgentConfig
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

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.provider = self._make_provider(config.provider)

    def _make_provider(self, provider_id: str):
        if mock_mode_enabled() or provider_id == "mock":
            return MockProvider()
        if provider_id == "deepseek":
            return DeepSeekProvider()
        if provider_id == "dashscope":
            return DashScopeProvider()
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
        return self.provider.chat_json(model=self.config.model, messages=msgs, schema=schema, temperature=temperature)

