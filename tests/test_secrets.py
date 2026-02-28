from __future__ import annotations

import os
import json
from pathlib import Path

from vibe.config import ProviderConfig
from vibe.secrets import apply_workspace_secrets, secrets_path


def test_apply_workspace_secrets_sets_env_when_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".vibe").mkdir(parents=True, exist_ok=True)

    p = secrets_path(tmp_path)
    p.write_text(json.dumps({"DEEPSEEK_API_KEY": "ds-test"}, ensure_ascii=False) + "\n", encoding="utf-8")

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    providers = {"deepseek": ProviderConfig(id="deepseek", base_url="https://api.deepseek.com/v1", api_key_env="DEEPSEEK_API_KEY")}

    applied = apply_workspace_secrets(tmp_path, providers=providers)
    assert os.getenv("DEEPSEEK_API_KEY") == "ds-test"
    assert applied.get("DEEPSEEK_API_KEY") == ".vibe/secrets.json"

