from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app
from vibe.storage.checkpoints import CheckpointsStore


def test_cli_run_mock_creates_green_checkpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    r2 = runner.invoke(app, ["task", "add", "hello"])
    assert r2.exit_code == 0, r2.output

    r3 = runner.invoke(app, ["run", "--mock", "--mock-writes"])
    assert r3.exit_code == 0, r3.output
    ckpt_id = r3.output.strip()

    cps = CheckpointsStore(tmp_path)
    cp = cps.get(ckpt_id)
    assert cp.green is True

    written = tmp_path / "hello.txt"
    assert written.exists()
    assert written.read_text(encoding="utf-8") == "hello from mock\n"
