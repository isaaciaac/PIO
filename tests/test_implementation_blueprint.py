from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app
from vibe.providers.base import ProviderMeta, ProviderResult
from vibe.providers.mock import MockProvider
from vibe.schemas import packs as schemas


def _ledger_events(repo_root: Path) -> list[dict]:
    out: list[dict] = []
    path = repo_root / ".vibe" / "ledger.jsonl"
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def test_implementation_blueprint_accepts_fix_agent_consult_aliases() -> None:
    bp = schemas.ImplementationBlueprint.model_validate(
        {
            "summary": "x",
            "allow": ["src/**"],
            "recommended_agent": "integration_engineer",
            "consult": ["architect", "env_engineer"],
            "reason": "cross-module fix",
        }
    )
    assert bp.global_allowed_write_globs == ["src/**"]
    assert bp.recommended_fix_agent == "integration_engineer"
    assert bp.consult_agents == ["architect", "env_engineer"]
    assert bp.escalation_reason == "cross-module fix"


def test_mock_blueprint_persists_recommended_fix_agent_and_consults(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    original = MockProvider.chat_json

    def patched(self, *, model, messages, schema, temperature=0.0, extra_body=None):
        if schema is schemas.ImplementationBlueprint:
            out = schemas.ImplementationBlueprint.model_validate(
                {
                    "summary": "lead blueprint",
                    "allow": ["src/**"],
                    "recommended_agent": "integration_engineer",
                    "consult": ["architect", "env_engineer", "bogus"],
                    "reason": "cross-module coordination required",
                }
            )
            meta = ProviderMeta(provider=self.provider_id, model="mock", usage={})
            return out, ProviderResult(raw_text=out.model_dump_json(), meta=meta)
        return original(self, model=model, messages=messages, schema=schema, temperature=temperature, extra_body=extra_body)

    monkeypatch.setattr(MockProvider, "chat_json", patched)

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    r2 = runner.invoke(app, ["task", "add", "build something"])
    assert r2.exit_code == 0, r2.output

    r3 = runner.invoke(app, ["run", "--mock", "--route", "L2"])
    assert r3.exit_code == 0, r3.output

    events = _ledger_events(tmp_path)
    bp_events = [e for e in events if e.get("type") == "LEAD_BLUEPRINT_BUILT"]
    assert bp_events
    pointer = str(bp_events[-1].get("pointers", [""])[0] or "")
    assert pointer

    payload = json.loads((tmp_path / pointer.split("@sha256:", 1)[0]).read_text(encoding="utf-8"))
    assert payload["recommended_fix_agent"] == "integration_engineer"
    assert payload["consult_agents"] == ["architect", "env_engineer"]
    assert payload["escalation_reason"] == "cross-module coordination required"

