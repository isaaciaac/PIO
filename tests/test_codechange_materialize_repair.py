from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app
from vibe.orchestrator import Orchestrator
from vibe.schemas import packs


class _StubAgent:
    def __init__(self) -> None:
        self.calls = 0

    def chat_json(self, *, schema, user: str, system=None, messages=None, temperature: float = 0.0):
        self.calls += 1
        payload = {
            "kind": "patch",
            "summary": "write docs to a safe path",
            "writes": [{"path": "docs/architecture.md", "content": "# ok\n"}],
            "files_changed": ["docs/architecture.md"],
            "blockers": [],
        }
        return schema.model_validate(payload), None


class _StubAgentFixMissingImport:
    def __init__(self) -> None:
        self.calls = 0

    def chat_json(self, *, schema, user: str, system=None, messages=None, temperature: float = 0.0):
        self.calls += 1
        payload = {
            "kind": "patch",
            "summary": "add missing imported file",
            "writes": [
                {"path": "src/a.ts", "content": "import { b } from './b';\nexport const a = b;\n"},
                {"path": "src/b.ts", "content": "export const b = 1;\n"},
            ],
            "files_changed": ["src/a.ts", "src/b.ts"],
            "blockers": [],
        }
        return schema.model_validate(payload), None


class _StubAgentFixMissingNpmDep:
    def __init__(self) -> None:
        self.calls = 0

    def chat_json(self, *, schema, user: str, system=None, messages=None, temperature: float = 0.0):
        self.calls += 1
        payload = {
            "kind": "patch",
            "summary": "add missing npm dependency",
            "writes": [
                {"path": "package.json", "content": json.dumps({"name": "x", "version": "0.0.0", "dependencies": {"axios": "^1.0.0"}}, ensure_ascii=False) + "\n"},
                {"path": "src/a.ts", "content": "import axios from 'axios';\nexport const a = axios;\n"},
            ],
            "files_changed": ["package.json", "src/a.ts"],
            "blockers": [],
        }
        return schema.model_validate(payload), None


def test_materialize_reprompts_on_internal_vibe_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    orch = Orchestrator(tmp_path)
    bad = packs.CodeChange.model_validate(
        {
            "kind": "patch",
            "summary": "bad internal write",
            "writes": [{"path": ".vibe/docs/architecture.md", "content": "# no\n"}],
            "files_changed": [".vibe/docs/architecture.md"],
        }
    )
    stub = _StubAgent()

    change, _ptrs = orch._materialize_code_change_with_repair(
        change=bad,
        actor_agent_id="coder_backend",
        actor=stub,
        actor_role="Backend Coder",
        workflow_hint="",
        max_repairs=1,
    )

    assert stub.calls == 1
    assert change.kind == "patch"
    assert (tmp_path / "docs" / "architecture.md").read_text(encoding="utf-8") == "# ok\n"


def test_materialize_reprompts_on_missing_relative_import_targets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    orch = Orchestrator(tmp_path)
    bad = packs.CodeChange.model_validate(
        {
            "kind": "patch",
            "summary": "bad missing module",
            "writes": [{"path": "src/a.ts", "content": "import { b } from './b';\nexport const a = b;\n"}],
            "files_changed": ["src/a.ts"],
        }
    )
    stub = _StubAgentFixMissingImport()

    change, _ptrs = orch._materialize_code_change_with_repair(
        change=bad,
        actor_agent_id="coder_backend",
        actor=stub,
        actor_role="Backend Coder",
        workflow_hint="",
        max_repairs=1,
    )

    assert stub.calls == 1
    assert change.kind == "patch"
    assert (tmp_path / "src" / "a.ts").exists()
    assert (tmp_path / "src" / "b.ts").exists()


def test_materialize_reprompts_on_missing_npm_dependency_declarations(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "version": "0.0.0", "dependencies": {}}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    orch = Orchestrator(tmp_path)
    bad = packs.CodeChange.model_validate(
        {
            "kind": "patch",
            "summary": "missing npm dep",
            "writes": [{"path": "src/a.ts", "content": "import axios from 'axios';\nexport const a = axios;\n"}],
            "files_changed": ["src/a.ts"],
        }
    )
    stub = _StubAgentFixMissingNpmDep()

    change, _ptrs = orch._materialize_code_change_with_repair(
        change=bad,
        actor_agent_id="coder_backend",
        actor=stub,
        actor_role="Backend Coder",
        workflow_hint="",
        max_repairs=1,
    )

    assert stub.calls == 1
    assert change.kind == "patch"
    pkg = json.loads((tmp_path / "package.json").read_text(encoding="utf-8", errors="replace"))
    deps = pkg.get("dependencies") or {}
    assert "axios" in deps
