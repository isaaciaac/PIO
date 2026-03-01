from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app
from vibe.orchestrator import Orchestrator
from vibe.schemas import packs


def _dummy_failed_report(cmd: str) -> packs.TestReport:
    return packs.TestReport(
        commands=[cmd],
        results=[packs.TestResult(command=cmd, returncode=1, passed=False, stdout="", stderr="")],
        passed=False,
        blockers=[f"Command failed: {cmd}"],
        pointers=[],
    )


def test_incident_capsule_detects_eslint_windows_single_quote_glob(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output

    (tmp_path / "server" / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "server" / "src" / "index.ts").write_text("export const x = 1;\n", encoding="utf-8")

    pkg = {
        "name": "x",
        "private": True,
        "scripts": {"lint": "eslint 'server/**/*.ts' --fix"},
        "devDependencies": {"eslint": "^8.57.0"},
    }
    (tmp_path / "package.json").write_text(json.dumps(pkg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    orch = Orchestrator(tmp_path)
    report = _dummy_failed_report("npm run lint")
    blocker = (
        "Oops! Something went wrong! :(\n\n"
        "ESLint: 8.57.1\n\n"
        "No files matching the pattern \"'server/**/*.ts'\" were found.\n"
        "Please check for typing mistakes in the pattern.\n"
    )
    incident = orch._incident_for_tests(report=report, blocker_text=blocker, activated_agents={"coder_backend"})
    assert incident.source == "tests"
    assert incident.category == "eslint_glob_quoting_windows"
    assert "Windows" in (" ".join(incident.diagnosis) or "")
    assert any("双引号" in s for s in (incident.next_steps or []))
    assert "eslint" in (incident.required_capabilities or [])
    assert any("package.json#L1-L" in p for p in (incident.evidence_pointers or []))

