from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app
from vibe.orchestrator import Orchestrator
from vibe.schemas import packs


def _mk_failed_report(cmd: str) -> packs.TestReport:
    return packs.TestReport(
        commands=[cmd],
        results=[packs.TestResult(command=cmd, returncode=1, passed=False, stdout="", stderr="")],
        passed=False,
        blockers=[f"Command failed: {cmd}"],
        pointers=[],
    )


def test_fix_coder_routes_backend_build_to_backend_coder(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIBE_MOCK_MODE", raising=False)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    orch = Orchestrator(tmp_path)
    report = _mk_failed_report('cd /d "backend" && npm run build')
    fix = orch._select_fix_coder_for_tests(report=report, blocker_text=report.blockers[0], activated_agents={"coder_backend", "coder_frontend"})
    assert fix == "coder_backend"


def test_fix_coder_routes_client_build_to_frontend_coder(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIBE_MOCK_MODE", raising=False)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    orch = Orchestrator(tmp_path)
    report = _mk_failed_report('cd /d "client" && npm run build')
    fix = orch._select_fix_coder_for_tests(report=report, blocker_text=report.blockers[0], activated_agents={"coder_backend", "coder_frontend"})
    assert fix == "coder_frontend"

