from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app
from vibe.orchestrator import Orchestrator
from vibe.schemas import packs


def test_fix_loop_budget_reserves_extra_for_smoke_to_full_escalation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    orch = Orchestrator(tmp_path)
    report = packs.TestReport(commands=["npm run build"], results=[], passed=False, blockers=["build failed"])

    max_loops = orch._compute_fix_loop_max_loops(
        base_max_loops=3,
        route_level="L4",
        report=report,
        started_smoke_preflight=True,
    )

    # L4 bumps to at least 6, plus 2 extra loops reserved for escalation blockers.
    assert max_loops >= 8

