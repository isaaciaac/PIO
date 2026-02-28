from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app
from vibe.scan import scan_is_stale


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
    assert "top_level_mtimes_ms" in payload
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


def test_scan_becomes_stale_when_top_level_dir_changes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(app, ["scan"])
    assert r2.exit_code == 0, r2.output

    assert scan_is_stale(tmp_path, max_age_s=999999) is False

    # Adding a file inside an existing top-level directory should refresh scan facts.
    (tmp_path / "src" / "new.txt").write_text("x\n", encoding="utf-8")
    assert scan_is_stale(tmp_path, max_age_s=999999) is True


def test_chat_semantic_force_rescans_repo(tmp_path: Path, monkeypatch) -> None:
    import time

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    (tmp_path / "README.md").write_text("# Demo\n\nhello\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.txt").write_text("hi\n", encoding="utf-8")

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(app, ["scan"])
    assert r2.exit_code == 0, r2.output

    state = tmp_path / ".vibe" / "manifests" / "scan_state.json"
    before = json.loads(state.read_text(encoding="utf-8"))["scanned_at"]

    # A generic greeting should not force a re-scan (scan remains up-to-date).
    r3 = runner.invoke(app, ["chat", "你好", "--mock", "--json"])
    assert r3.exit_code == 0, r3.output
    after1 = json.loads(state.read_text(encoding="utf-8"))["scanned_at"]
    assert after1 == before

    # A repo-structure question forces a refresh even if TTL hasn't expired.
    time.sleep(0.01)
    r4 = runner.invoke(app, ["chat", "目录结构是什么？", "--mock", "--json"])
    assert r4.exit_code == 0, r4.output
    after2 = json.loads(state.read_text(encoding="utf-8"))["scanned_at"]
    assert after2 != after1


def test_scan_ignores_nested_node_modules(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"name":"demo","scripts":{"test":"echo ok"}}\n', encoding="utf-8")
    (tmp_path / "client" / "node_modules").mkdir(parents=True)
    (tmp_path / "client" / "node_modules" / "a.txt").write_text("x\n", encoding="utf-8")
    (tmp_path / "client" / "src").mkdir(parents=True)
    (tmp_path / "client" / "src" / "main.ts").write_text("export const x = 1;\n", encoding="utf-8")

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    r2 = runner.invoke(app, ["scan"])
    assert r2.exit_code == 0, r2.output

    index_path = tmp_path / ".vibe" / "manifests" / "repo_index.json"
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    paths = [str(p) for p in (payload.get("paths_sample") or [])]
    assert paths
    assert all("node_modules" not in p.replace("\\", "/") for p in paths)
