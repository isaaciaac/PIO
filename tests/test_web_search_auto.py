from __future__ import annotations

from typing import Any, Dict, List, Optional, Type, TypeVar

import pytest
from pydantic import BaseModel, Field, create_model

from vibe.agents.base import BaseAgent
from vibe.config import AgentConfig, AgentMemoryScope, ProviderConfig
from vibe.providers.base import ProviderResult
from vibe.providers.base import ProviderMeta


T = TypeVar("T", bound=BaseModel)


class _ProviderNoExtraBody:
    def chat_json(  # type: ignore[no-untyped-def]
        self,
        *,
        model: str,
        messages: List[Dict[str, str]],
        schema: Type[T],
        temperature: float = 0.0,
    ) -> tuple[T, ProviderResult]:
        out = schema.model_validate({})
        meta = ProviderMeta(provider="dashscope", model=model, usage={})
        return out, ProviderResult(raw_text=out.model_dump_json(), meta=meta)


class _ProviderWithExtraBody:
    def __init__(self) -> None:
        self.extra_bodies: list[Optional[Dict[str, Any]]] = []

    def chat_json(  # type: ignore[no-untyped-def]
        self,
        *,
        model: str,
        messages: List[Dict[str, str]],
        schema: Type[T],
        temperature: float = 0.0,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> tuple[T, ProviderResult]:
        self.extra_bodies.append(extra_body)
        out = schema.model_validate({})
        meta = ProviderMeta(provider="dashscope", model=model, usage={})
        return out, ProviderResult(raw_text=out.model_dump_json(), meta=meta)


def _mk_agent_cfg(*, agent_id: str, web_search: Any) -> AgentConfig:
    return AgentConfig(
        id=agent_id,
        enabled=True,
        provider="dashscope",
        model="qwen-plus",
        web_search=web_search,
        purpose="test",
        capabilities=[],
        io_schema="x",
        memory_scope=AgentMemoryScope(view_dir=".vibe/views/test", ledger_read_filter=[], ledger_write_types=[]),
        tools_allowed=[],
        rollback_strategy="",
        prompt_template="",
    )


def test_web_search_off_does_not_pass_extra_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIBE_MOCK_MODE", raising=False)
    cfg = _mk_agent_cfg(agent_id="router", web_search="off")
    provs = {"dashscope": ProviderConfig(id="dashscope", base_url="http://example", api_key_env="X")}
    agent = BaseAgent(cfg, providers=provs)
    agent.provider = _ProviderNoExtraBody()

    Dummy = create_model("Dummy", ok=(bool, Field(default=True)))
    agent.chat_json(schema=Dummy, user="请确认这个 API endpoint 是否正确：base_url=... 文档在哪？")


def test_web_search_on_passes_extra_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIBE_MOCK_MODE", raising=False)
    cfg = _mk_agent_cfg(agent_id="router", web_search="on")
    provs = {"dashscope": ProviderConfig(id="dashscope", base_url="http://example", api_key_env="X")}
    agent = BaseAgent(cfg, providers=provs)
    p = _ProviderWithExtraBody()
    agent.provider = p

    Dummy = create_model("Dummy", ok=(bool, Field(default=True)))
    agent.chat_json(schema=Dummy, user="hello")
    assert p.extra_bodies and p.extra_bodies[-1] == {"enable_search": True}


def test_web_search_auto_only_when_verification_intent_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIBE_MOCK_MODE", raising=False)
    cfg = _mk_agent_cfg(agent_id="router", web_search="auto")
    provs = {"dashscope": ProviderConfig(id="dashscope", base_url="http://example", api_key_env="X")}
    agent = BaseAgent(cfg, providers=provs)
    p = _ProviderWithExtraBody()
    agent.provider = p

    Dummy = create_model("Dummy", ok=(bool, Field(default=True)))
    agent.chat_json(schema=Dummy, user="帮我写个 README")  # no verification intent -> no search
    assert p.extra_bodies and p.extra_bodies[-1] is None

    agent.chat_json(schema=Dummy, user="请确认这个 API endpoint 是否正确：base_url=...，看下文档")  # should search
    assert p.extra_bodies[-1] == {"enable_search": True}


def test_web_search_auto_disabled_for_coder_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIBE_MOCK_MODE", raising=False)
    cfg = _mk_agent_cfg(agent_id="coder_backend", web_search="auto")
    provs = {"dashscope": ProviderConfig(id="dashscope", base_url="http://example", api_key_env="X")}
    agent = BaseAgent(cfg, providers=provs)
    p = _ProviderWithExtraBody()
    agent.provider = p

    Dummy = create_model("Dummy", ok=(bool, Field(default=True)))
    agent.chat_json(schema=Dummy, user="请确认这个 API endpoint 是否正确：base_url=...，看下文档")
    assert p.extra_bodies and p.extra_bodies[-1] is None
