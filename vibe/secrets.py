from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from vibe.config import ProviderConfig


def secrets_path(repo_root: Path) -> Path:
    return repo_root / ".vibe" / "secrets.json"


def load_secrets(repo_root: Path) -> Dict[str, str]:
    """
    Loads workspace secrets from `.vibe/secrets.json`.

    Expected format (recommended):
      {
        "DEEPSEEK_API_KEY": "…",
        "DASHSCOPE_API_KEY": "…"
      }

    Notes:
    - `.vibe/` is gitignored by default in this repo, but this is still plaintext on disk.
    """
    p = secrets_path(repo_root)
    if not p.exists():
        return {}
    try:
        payload: Any = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in payload.items():
        if not isinstance(k, str):
            continue
        if not isinstance(v, str):
            continue
        s = v.strip()
        if not s:
            continue
        out[k.strip()] = s
    return out


def apply_workspace_secrets(repo_root: Path, *, providers: Dict[str, ProviderConfig]) -> Dict[str, str]:
    """
    Best-effort: if env vars are missing, fill them from `.vibe/secrets.json`.
    Returns a map of env var names that were set (name -> source).
    """
    secrets = load_secrets(repo_root)
    applied: Dict[str, str] = {}
    if not secrets:
        return applied

    for p in providers.values():
        env_name = (p.api_key_env or "").strip()
        if not env_name:
            continue
        if os.getenv(env_name):
            continue
        v = secrets.get(env_name)
        if not v:
            continue
        os.environ[env_name] = v
        applied[env_name] = ".vibe/secrets.json"

    return applied

