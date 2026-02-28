from __future__ import annotations

import os
import time
from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app
from vibe.orchestrator import Orchestrator


def test_node_install_needed_when_package_json_newer_than_lockfile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIBE_MOCK_MODE", raising=False)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    client = tmp_path / "client"
    client.mkdir(parents=True, exist_ok=True)

    pkg = client / "package.json"
    lock = client / "package-lock.json"
    (client / "node_modules").mkdir(parents=True, exist_ok=True)

    pkg.write_text(
        '{"name":"demo","scripts":{"build":"echo ok"},"dependencies":{"axios":"^1.0.0"}}\n',
        encoding="utf-8",
    )
    lock.write_text('{"name":"demo"}\n', encoding="utf-8")

    now = time.time()
    os.utime(lock, (now - 120, now - 120))
    os.utime(pkg, (now, now))

    orch = Orchestrator(tmp_path)
    needed, reason = orch._node_install_needed(Path("client"))
    assert needed is True
    assert reason == "package_json_newer_than_lockfile"


def test_node_install_needed_when_dependency_folder_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIBE_MOCK_MODE", raising=False)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    client = tmp_path / "client"
    client.mkdir(parents=True, exist_ok=True)

    pkg = client / "package.json"
    lock = client / "package-lock.json"
    node_modules = client / "node_modules"
    node_modules.mkdir(parents=True, exist_ok=True)

    pkg.write_text(
        '{"name":"demo","scripts":{"build":"echo ok"},"dependencies":{"axios":"^1.0.0","react":"^18.0.0"}}\n',
        encoding="utf-8",
    )
    lock.write_text('{"name":"demo"}\n', encoding="utf-8")

    # Lockfile is newer so we don't trigger the mtime fast-path.
    now = time.time()
    os.utime(pkg, (now - 60, now - 60))
    os.utime(lock, (now, now))

    # Pretend react is installed, axios is missing.
    (node_modules / "react").mkdir(parents=True, exist_ok=True)

    orch = Orchestrator(tmp_path)
    needed, reason = orch._node_install_needed(Path("client"))
    assert needed is True
    assert reason.startswith("missing_dep:")
    assert "axios" in reason


def test_determine_test_commands_includes_multiple_node_projects(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIBE_MOCK_MODE", raising=False)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    for d in ["backend", "client"]:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
        (tmp_path / d / "package.json").write_text(
            '{"name":"x","scripts":{"build":"echo ok"}}\n',
            encoding="utf-8",
        )

    orch = Orchestrator(tmp_path)
    cmds = orch._determine_test_commands(profile="unit")
    assert any(("backend" in c) and ("run build" in c or " run build" in c or "build" in c) for c in cmds)
    assert any(("client" in c) and ("run build" in c or " run build" in c or "build" in c) for c in cmds)
