from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app
from vibe.config import VibeConfig, write_default_config
from vibe.storage.checkpoints import CheckpointsStore


def test_run_creates_non_green_checkpoint_when_fix_loop_exhausted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIBE_MOCK_MODE", raising=False)
    monkeypatch.delenv("VIBE_MOCK_WRITES", raising=False)
    runner = CliRunner()

    # Create a failing pytest test so the workflow cannot reach green.
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_fail.py").write_text("def test_fail():\n    assert False\n", encoding="utf-8")

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    # Use mock providers without enabling VIBE_MOCK_MODE, so tests still run locally.
    cfg_path = tmp_path / ".vibe" / "vibe.yaml"
    cfg = VibeConfig.load(cfg_path)
    for agent in cfg.agents.values():
        agent.provider = "mock"
        agent.model = "mock"
    write_default_config(tmp_path, cfg)

    r2 = runner.invoke(app, ["task", "add", "trigger failing tests"])
    assert r2.exit_code == 0, r2.output
    task_id = r2.output.strip()

    # Force L1 to avoid hard-rule upgrades for auth/contract keywords.
    r3 = runner.invoke(app, ["run", "--task", task_id, "--route", "L1"])
    assert r3.exit_code == 0, r3.output
    ckpt_id = r3.output.strip()

    cps = CheckpointsStore(tmp_path)
    cp = cps.get(ckpt_id)
    assert cp.green is False
    assert cp.meta.get("reason") == "fix_loop_blockers"
    assert cp.meta.get("blockers")
