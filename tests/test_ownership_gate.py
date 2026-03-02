from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app
from vibe.orchestrator import Orchestrator
from vibe.schemas import packs


def _load_events(path: Path) -> list[dict]:
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def test_owned_paths_require_router_approval_then_apply(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VIBE_MOCK_MODE", "1")

    runner = CliRunner()
    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    orch = Orchestrator(tmp_path)
    change = packs.CodeChange.model_validate(
        {
            "kind": "patch",
            "summary": "attempt to change owned contract file",
            "writes": [
                {"path": "src/types.ts", "content": "export interface X { id: string }\n"},
                {"path": "src/ok.ts", "content": "export const ok = true;\n"},
            ],
            "files_changed": ["src/types.ts", "src/ok.ts"],
            "blockers": [],
        }
    )

    activated = {"router", "coder_backend"}

    def activate_agent(agent_id: str, *, reason: str) -> None:
        _ = reason
        activated.add(agent_id)

    out, _ptrs = orch._materialize_code_change(
        change,
        actor_agent_id="coder_backend",
        activated_agents=activated,
        activate_agent=activate_agent,
        route_level="L1",
        style="balanced",
    )

    assert out.kind == "patch"
    assert (tmp_path / "src" / "types.ts").read_text(encoding="utf-8") == "export interface X { id: string }\n"
    assert (tmp_path / "src" / "ok.ts").read_text(encoding="utf-8") == "export const ok = true;\n"

    events = _load_events(tmp_path / ".vibe" / "ledger.jsonl")
    types = [e.get("type") for e in events]
    assert "OWNERSHIP_CHANGE_REQUESTED" in types
    assert "OWNERSHIP_CHANGE_APPROVED" in types

