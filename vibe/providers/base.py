from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Type, TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError


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


def _unwrap_schema_envelope(data: Any, *, schema: Type[BaseModel]) -> Any:
    if not isinstance(data, dict):
        return data

    schema_name = schema.__name__
    # Common model behavior: wrap the payload under the schema name.
    if schema_name in data:
        v = data[schema_name]
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                inner = _extract_json(v)
                if isinstance(inner, dict):
                    return inner
            except Exception:
                pass

    # Case-insensitive match.
    for k, v in data.items():
        if isinstance(k, str) and k.lower() == schema_name.lower() and isinstance(v, dict):
            return v
        if isinstance(k, str) and k.lower() == schema_name.lower() and isinstance(v, str):
            try:
                inner = _extract_json(v)
                if isinstance(inner, dict):
                    return inner
            except Exception:
                pass

    # Another common wrapper key.
    if "data" in data and isinstance(data["data"], dict) and len(data) == 1:
        return data["data"]
    if "data" in data and isinstance(data["data"], str) and len(data) == 1:
        try:
            inner = _extract_json(data["data"])
            if isinstance(inner, dict):
                return inner
        except Exception:
            pass

    return data


def _parse_json_to_schema(text: str, *, schema: Type[T]) -> T:
    data = _extract_json(text)
    data = _unwrap_schema_envelope(data, schema=schema)
    return schema.model_validate(data)


class OpenAICompatProvider:
    def __init__(self, *, provider_id: str, base_url: str, api_key_env: Optional[str]) -> None:
        self.provider_id = provider_id
        self.base_url = base_url
        self.api_key_env = api_key_env

    def _api_key(self) -> str:
        if not self.api_key_env:
            raise ProviderError(f"{self.provider_id} requires no api key env, but _api_key() was called")
        # Common misconfig: user puts the *actual key* into api_key_env.
        if self.api_key_env.startswith("sk-") or self.api_key_env.startswith("ds-") or len(self.api_key_env) > 40:
            raise ProviderError(
                f"Invalid api_key_env for provider {self.provider_id}: it looks like an API key, not an env var name. "
                f"Set api_key_env to something like 'DEEPSEEK_API_KEY'/'DASHSCOPE_API_KEY', and put the real key into that env var."
            )
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
        try:
            parsed = _parse_json_to_schema(content, schema=schema)
        except (json.JSONDecodeError, ValidationError) as e:
            # Best-effort single repair pass: ask the model to output valid JSON for the target schema.
            fields = list(getattr(schema, "model_fields", {}).keys())
            repair_system = (
                "You are a JSON repair tool. Output JSON only. "
                "Do not wrap the object in an extra top-level key."
            )
            repair_user = (
                f"Target schema: {schema.__name__}\n"
                f"Required fields: {fields}\n\n"
                f"Validation/parse error:\n{e}\n\n"
                "Fix the following model output to valid JSON that matches the schema exactly.\n\n"
                f"Bad output:\n{content}"
            )
            repair_msgs = self.normalize_messages(
                [{"role": "system", "content": repair_system}, {"role": "user", "content": repair_user}],
                model=model,
            )
            repair_resp = client.chat.completions.create(model=model, messages=repair_msgs, temperature=0.0)
            repaired = repair_resp.choices[0].message.content or ""
            parsed = _parse_json_to_schema(repaired, schema=schema)
            content = repaired
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
