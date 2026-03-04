from __future__ import annotations

import json
import os
import re
import ast
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
    """
    Backward-compatible JSON extraction.

    Prefer using `_parse_json_to_schema()` which can try multiple candidate JSON
    fragments and validate against a schema.
    """
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


def _extract_fenced_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    for m in re.finditer(r"```(?:json)?\s*(?P<body>[\s\S]*?)```", text, flags=re.IGNORECASE):
        body = (m.group("body") or "").strip()
        if body:
            blocks.append(body)
    return blocks


def _iter_balanced_json_substrings(text: str, *, max_candidates: int = 20) -> list[str]:
    """
    Extract balanced JSON object/array substrings from a larger text blob.

    This is intentionally permissive: models sometimes emit a valid JSON object
    followed by extra prose, or multiple JSON objects.
    """
    out: list[str] = []
    if not text:
        return out

    # Limit scanning to keep worst-case runtime bounded.
    scan = text if len(text) <= 200_000 else text[:200_000]
    starts: list[int] = []
    for i, ch in enumerate(scan):
        if ch in "{[":
            starts.append(i)
            if len(starts) >= 80:
                break

    for start in starts:
        opener = scan[start]
        stack: list[str] = [opener]
        in_str = False
        esc = False
        for j in range(start + 1, len(scan)):
            c = scan[j]
            if in_str:
                if esc:
                    esc = False
                    continue
                if c == "\\":
                    esc = True
                    continue
                if c == '"':
                    in_str = False
                continue

            if c == '"':
                in_str = True
                continue
            if c in "{[":
                stack.append(c)
                continue
            if c in "}]":
                if not stack:
                    break
                open_ch = stack.pop()
                if (open_ch == "{" and c != "}") or (open_ch == "[" and c != "]"):
                    break
                if not stack:
                    frag = scan[start : j + 1].strip()
                    if frag:
                        out.append(frag)
                    break

        if len(out) >= max_candidates:
            break

    # De-dup (preserve order).
    seen: set[str] = set()
    uniq: list[str] = []
    for s in out:
        if s in seen:
            continue
        seen.add(s)
        uniq.append(s)
    return uniq


def _strip_js_comments(text: str) -> str:
    # Best-effort: strip line comments. Keep this conservative to avoid breaking URLs etc.
    lines = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if s.startswith("//"):
            continue
        lines.append(ln)
    return "\n".join(lines)


def _quote_unquoted_object_keys(text: str) -> str:
    """
    Convert JS-like object keys into JSON keys:
      {kind: "x"} -> {"kind": "x"}
    """
    s = text or ""
    # This is heuristic; it intentionally doesn't try to parse strings/escapes perfectly.
    return re.sub(r'([{\[,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)', r'\1"\2"\3', s)


def _remove_trailing_commas(text: str) -> str:
    # { "a": 1, } -> { "a": 1 }
    # [1,2,] -> [1,2]
    return re.sub(r",(\s*[}\]])", r"\1", text or "")


def _try_json_loads_relaxed(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Relaxed fixes for common model output formats.
    fixed = (text or "").strip()
    fixed = _strip_js_comments(fixed)
    fixed = fixed.strip().rstrip(";").strip()
    fixed = _quote_unquoted_object_keys(fixed)
    fixed = _remove_trailing_commas(fixed)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        return None


def _try_python_literal(text: str) -> Any:
    """
    Parse Python-dict-like output safely:
      {'a': 1, 'b': True} -> dict

    This is a deterministic fallback for models that emit single-quoted "JSON".
    """
    s = (text or "").strip()
    if not s:
        return None
    s = _strip_js_comments(s).strip().rstrip(";").strip()
    try:
        val = ast.literal_eval(s)
    except Exception:
        # Some models include JSON booleans/null inside a Python-ish object.
        try:
            s2 = re.sub(r"\bnull\b", "None", s, flags=re.IGNORECASE)
            s2 = re.sub(r"\btrue\b", "True", s2, flags=re.IGNORECASE)
            s2 = re.sub(r"\bfalse\b", "False", s2, flags=re.IGNORECASE)
            val = ast.literal_eval(s2)
        except Exception:
            return None
    if isinstance(val, (dict, list)):
        return val
    return None


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


def _coerce_data_to_schema(data: Any, *, schema: Type[BaseModel]) -> Any:
    """
    Deterministic, schema-specific coercions for common partial outputs.

    This is a safety net so the workflow can continue even when a model outputs
    only a fragment of the expected schema (e.g. a single file write instead of a
    full CodeChange object).
    """

    name = getattr(schema, "__name__", "")

    if name == "CodeChange":
        # Models sometimes return only a single file write: {path, content} (or {file, text}).
        if isinstance(data, dict):
            has_kind = isinstance(data.get("kind") or data.get("type"), str)
            has_summary = isinstance(data.get("summary") or data.get("message"), str)
            if has_kind and has_summary:
                return data

            path = None
            for k in ("path", "file", "filepath", "filename", "name"):
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    path = v.strip()
                    break

            content = None
            for k in ("content", "text", "contents", "body", "value"):
                v = data.get(k)
                if isinstance(v, str):
                    content = v
                    break

            if path is not None and content is not None and "writes" not in data:
                summary = (data.get("summary") or data.get("message") or f"Write {path}").strip()
                return {
                    "kind": "patch",
                    "summary": summary[:240] if isinstance(summary, str) else f"Write {path}",
                    "writes": [data],
                    "files_changed": [path],
                    "blockers": [],
                }

            # If it looks like a "writes" object but missing kind/summary, fill the minimum.
            if "writes" in data and isinstance(data.get("writes"), list) and not has_kind:
                summary = data.get("summary") or data.get("message") or "Apply file writes"
                return {
                    **data,
                    "kind": "patch",
                    "summary": str(summary)[:240],
                }

            if not has_summary and isinstance(data.get("title"), str):
                return {**data, "summary": data.get("title")}

        if isinstance(data, list):
            # Treat a top-level list as writes[] when elements look like file writes.
            looks_like_writes = True
            paths: list[str] = []
            for it in data[:20]:
                if not isinstance(it, dict):
                    looks_like_writes = False
                    break
                p = None
                for k in ("path", "file", "filepath", "filename", "name"):
                    v = it.get(k)
                    if isinstance(v, str) and v.strip():
                        p = v.strip()
                        break
                if not p:
                    looks_like_writes = False
                    break
                paths.append(p)
            if looks_like_writes:
                summary = f"Write {len(paths)} file(s)"
                if len(paths) == 1:
                    summary = f"Write {paths[0]}"
                return {"kind": "patch", "summary": summary, "writes": data, "files_changed": paths, "blockers": []}

    if name == "Plan":
        # Some models return tasks[] directly.
        if isinstance(data, list):
            return {"tasks": data}
        # Some return {plan:{tasks:[...]}}.
        if isinstance(data, dict) and "tasks" not in data:
            v = data.get("plan")
            if isinstance(v, dict) and "tasks" in v:
                return v

    if name == "RequirementPack":
        # Allow summary aliases.
        if isinstance(data, dict) and "summary" not in data:
            for k in ("title", "overview", "scope"):
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    return {**data, "summary": v.strip()}

    return data


def _parse_json_to_schema(text: str, *, schema: Type[T]) -> T:
    """
    Parse and validate model output against a schema.

    Models are expected to output a single JSON object, but in practice they may:
    - wrap the payload under a schema name
    - include extra commentary before/after JSON
    - emit multiple JSON objects
    This function tries multiple candidates deterministically and returns the
    first one that validates.
    """

    raw = (text or "").strip()
    if not raw:
        raise json.JSONDecodeError("Empty content", raw, 0)

    candidates: list[str] = []
    candidates.append(raw)
    candidates.extend(_extract_fenced_blocks(raw))
    candidates.extend(_iter_balanced_json_substrings(raw))

    last_err: Exception | None = None
    for cand in candidates[:60]:
        if not cand:
            continue
        data = _try_json_loads_relaxed(cand)
        if data is None:
            data = _try_python_literal(cand)
        if data is None:
            last_err = json.JSONDecodeError("No parseable JSON", cand, 0)
            continue
        try:
            data = _unwrap_schema_envelope(data, schema=schema)
            try:
                return schema.model_validate(data)
            except ValidationError:
                coerced = _coerce_data_to_schema(data, schema=schema)
                if coerced is not data:
                    return schema.model_validate(coerced)
                raise
        except ValidationError as e:
            last_err = e
            continue

    if last_err is not None:
        raise last_err
    raise json.JSONDecodeError("No JSON candidates found", raw, 0)


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
