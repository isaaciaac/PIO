from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app
from vibe.routes import DiffStats, decide_route
from vibe.storage.checkpoints import CheckpointsStore


def _ledger_types(repo_root: Path) -> list[str]:
    path = repo_root / ".vibe" / "ledger.jsonl"
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(str(json.loads(line).get("type") or ""))
        except json.JSONDecodeError:
            continue
    return out


def test_route_decider_defaults_to_L1_for_low_risk_task() -> None:
    d = decide_route(task_text="hello", diff=DiffStats(), recent_test_fail_count=0, requested_level=None)
    assert d.route_level == "L1"


def test_route_decider_honors_explicit_L0_when_safe() -> None:
    d = decide_route(task_text="hello", diff=DiffStats(), recent_test_fail_count=0, requested_level="L0")
    assert d.route_level == "L0"


def test_route_decider_escalates_to_L4_for_migration_even_if_L0_requested() -> None:
    d = decide_route(task_text="数据库迁移：新增字段并提供回滚脚本", diff=DiffStats(), recent_test_fail_count=0, requested_level="L0")
    assert d.route_level == "L4"


def test_cli_run_mock_route_L0_creates_draft_checkpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    r2 = runner.invoke(app, ["task", "add", "hello"])
    assert r2.exit_code == 0, r2.output

    r3 = runner.invoke(app, ["run", "--mock", "--route", "L0"])
    assert r3.exit_code == 0, r3.output
    ckpt_id = r3.output.strip()

    cps = CheckpointsStore(tmp_path)
    cp = cps.get(ckpt_id)
    assert cp.green is False
    assert cp.meta.get("route_level") == "L0"
    assert cp.meta.get("draft") is True
    assert "router" in (cp.meta.get("agents") or [])

    types = _ledger_types(tmp_path)
    assert "ROUTE_SELECTED" in types
    assert "AGENTS_ACTIVATED" in types
    assert "CHECKPOINT_CREATED" in types


def test_cli_run_mock_route_auto_creates_green_checkpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    r2 = runner.invoke(app, ["task", "add", "hello"])
    assert r2.exit_code == 0, r2.output

    r3 = runner.invoke(app, ["run", "--mock"])
    assert r3.exit_code == 0, r3.output
    ckpt_id = r3.output.strip()

    cps = CheckpointsStore(tmp_path)
    cp = cps.get(ckpt_id)
    assert cp.green is True
    assert cp.meta.get("route_level") == "L1"
    assert "pm" in (cp.meta.get("agents") or [])

    types = _ledger_types(tmp_path)
    assert "ROUTE_SELECTED" in types
    assert "AGENTS_ACTIVATED" in types
    assert "CHECKPOINT_CREATED" in types


def test_cli_run_mock_route_L2_runs_review_and_creates_green_checkpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    r2 = runner.invoke(app, ["task", "add", "hello"])
    assert r2.exit_code == 0, r2.output

    r3 = runner.invoke(app, ["run", "--mock", "--route", "L2"])
    assert r3.exit_code == 0, r3.output
    ckpt_id = r3.output.strip()

    cps = CheckpointsStore(tmp_path)
    cp = cps.get(ckpt_id)
    assert cp.green is True
    assert cp.meta.get("route_level") == "L2"

    types = _ledger_types(tmp_path)
    assert "USECASES_DEFINED" in types
    assert "ADR_ADDED" in types
    assert "REVIEW_PASSED" in types
