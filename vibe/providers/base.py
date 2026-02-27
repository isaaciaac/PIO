from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Type, TypeVar

from openai import OpenAI
from pydantic import BaseModel


T = TypeVar("T", bound=BaseModel)


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderMeta:
    provider: str
    model: str
    usage: Dict[str, Any]


@dataclass(frozen=True)
class ProviderResult:
    raw_text: str
    meta: ProviderMeta


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        # Best-effort strip fenced block
        parts = text.split("```")
        if len(parts) >= 3:
            text = parts[1]
            if text.lstrip().startswith("json"):
                text = text.lstrip()[4:]
            text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


class OpenAICompatProvider:
    def __init__(self, *, provider_id: str, base_url: str, api_key_env: Optional[str]) -> None:
        self.provider_id = provider_id
        self.base_url = base_url
        self.api_key_env = api_key_env

    def _api_key(self) -> str:
        if not self.api_key_env:
            raise ProviderError(f"{self.provider_id} requires no api key env, but _api_key() was called")
        key = os.getenv(self.api_key_env)
        if not key:
            raise ProviderError(f"Missing env var {self.api_key_env} for provider {self.provider_id}")
        return key

    def _client(self) -> OpenAI:
        return OpenAI(api_key=self._api_key(), base_url=self.base_url)

    def normalize_messages(self, messages: List[Dict[str, str]], *, model: str) -> List[Dict[str, str]]:
        return messages

    def chat_json(self, *, model: str, messages: List[Dict[str, str]], schema: Type[T], temperature: float = 0.0) -> tuple[T, ProviderResult]:
        client = self._client()
        msgs = self.normalize_messages(messages, model=model)
        resp = client.chat.completions.create(model=model, messages=msgs, temperature=temperature)
        content = resp.choices[0].message.content or ""
        data = _extract_json(content)
        parsed = schema.model_validate(data)
        usage = getattr(resp, "usage", None)
        usage_dict = usage.model_dump() if usage is not None else {}
        return parsed, ProviderResult(raw_text=content, meta=ProviderMeta(provider=self.provider_id, model=model, usage=usage_dict))


def ensure_deepseek_reasoner_format(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    # DeepSeek reasoner requires the first non-system message to be user.
    out: List[Dict[str, str]] = []
    i = 0
    while i < len(messages) and messages[i].get("role") == "system":
        out.append(messages[i])
        i += 1
    if i < len(messages) and messages[i].get("role") != "user":
        out.append({"role": "user", "content": "Context follows."})
    out.extend(messages[i:])
    return out

