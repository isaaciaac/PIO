from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app
from vibe.orchestrator import Orchestrator


def test_determine_test_commands_does_not_treat_tests_dir_as_python(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIBE_MOCK_MODE", raising=False)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    # Node project with a tests/ folder (common). No Python markers.
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "x.test.ts").write_text("test('x', () => expect(1).toBe(1));\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"name":"x","scripts":{"build":"echo ok","test":"echo ok"}}\n', encoding="utf-8")

    orch = Orchestrator(tmp_path)
    cmds = orch._determine_test_commands(profile="full")

    assert cmds, "expected some node commands"
    assert all("python -m" not in c for c in cmds)

