from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app


def test_cli_scan_writes_outputs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    (tmp_path / "README.md").write_text("# Demo\n\nhello\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"name":"demo","scripts":{"test":"echo ok"}}\n', encoding="utf-8")

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    r2 = runner.invoke(app, ["scan"])
    assert r2.exit_code == 0, r2.output

    state = tmp_path / ".vibe" / "manifests" / "scan_state.json"
    overview = tmp_path / ".vibe" / "manifests" / "repo_overview.md"
    index = tmp_path / ".vibe" / "manifests" / "repo_index.json"
    assert state.exists()
    assert overview.exists()
    assert index.exists()

    payload = json.loads(state.read_text(encoding="utf-8"))
    assert "scanned_at" in payload
    assert payload.get("file_count", 0) > 0


def test_chat_triggers_auto_scan(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    (tmp_path / "README.md").write_text("# Demo\n\nRun: npm test\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"name":"demo","scripts":{"test":"echo ok"}}\n', encoding="utf-8")

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    state = tmp_path / ".vibe" / "manifests" / "scan_state.json"
    assert not state.exists()

    r2 = runner.invoke(app, ["chat", "这个项目是做什么的？", "--mock", "--json"])
    assert r2.exit_code == 0, r2.output
    assert state.exists()

