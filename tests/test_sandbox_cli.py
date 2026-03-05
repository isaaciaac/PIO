from __future__ import annotations

from typer.testing import CliRunner

from vibe.cli import app


def test_sandbox_cli_runs(tmp_path, monkeypatch) -> None:
    # Run sandbox without verification to keep tests fast and deterministic.
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    r = runner.invoke(app, ["sandbox", "--no-verify", "--clean"])
    assert r.exit_code == 0, r.output
    assert "sandbox created:" in r.output
    assert "checkpoint:" in r.output
