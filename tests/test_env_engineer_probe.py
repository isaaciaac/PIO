from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from vibe.cli import app
from vibe.providers.base import ProviderMeta, ProviderResult
from vibe.providers.mock import MockProvider
from vibe.schemas import packs as schemas
from vibe.storage.checkpoints import CheckpointsStore


def _ledger_events(repo_root: Path) -> list[dict]:
    path = repo_root / ".vibe" / "ledger.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def test_env_engineer_probes_when_no_qa_commands(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    monkeypatch.delenv("VIBE_MOCK_MODE", raising=False)
    monkeypatch.delenv("VIBE_MOCK_WRITES", raising=False)

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    # Configure all agents to use the built-in mock provider without enabling global mock mode.
    # This lets us exercise the env_engineer probe path (which is skipped when VIBE_MOCK_MODE=1).
    cfg_path = tmp_path / ".vibe" / "vibe.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    for a in (cfg.get("agents") or {}).values():
        a["provider"] = "mock"
        a["model"] = "mock"
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")

    r2 = runner.invoke(app, ["task", "add", "hello"])
    assert r2.exit_code == 0, r2.output

    # Patch MockProvider to return a runnable EnvSpec so the workflow can proceed to green.
    orig = MockProvider.chat_json

    def patched_chat_json(self, *, model, messages, schema, temperature=0.0):  # type: ignore[no-untyped-def]
        if schema is schemas.EnvSpec:
            out = schemas.EnvSpec(commands=["echo envspec-ok"])
            meta = ProviderMeta(provider="mock", model="mock", usage={})
            return out, ProviderResult(raw_text=out.model_dump_json(), meta=meta)
        return orig(self, model=model, messages=messages, schema=schema, temperature=temperature)

    monkeypatch.setattr(MockProvider, "chat_json", patched_chat_json)

    r3 = runner.invoke(app, ["run", "--route", "L1"])
    assert r3.exit_code == 0, r3.output
    ckpt_id = r3.output.strip()

    cps = CheckpointsStore(tmp_path)
    cp = cps.get(ckpt_id)
    assert cp.green is True

    evts = _ledger_events(tmp_path)
    assert any(e.get("type") == "ENV_PROBED" for e in evts)
    assert any(
        e.get("type") == "TEST_RUN" and "echo envspec-ok" in (e.get("meta") or {}).get("commands", [])
        for e in evts
    )
