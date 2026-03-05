from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vibe.cli import app
from vibe.orchestrator import Orchestrator
from vibe.schemas import packs


def test_auto_fix_prefers_cmd_when_bin_exe_missing(tmp_path: Path, monkeypatch) -> None:
    if os.name != "nt":
        pytest.skip("Windows-specific npm shim behavior")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "node_modules" / ".bin").mkdir(parents=True, exist_ok=True)

    # Only `.cmd` exists; `.exe` is missing (a valid Windows/npm layout for some tools).
    (tmp_path / "node_modules" / ".bin" / "tool.cmd").write_text("@echo off\r\necho ok\r\n", encoding="utf-8")

    script_path = tmp_path / "scripts" / "run-tool.js"
    script_path.write_text(
        "const bin = 'node_modules/.bin/tool.exe';\n"
        "const manual = 'bin/tool.exe';\n"
        "console.log(bin, manual);\n",
        encoding="utf-8",
    )

    orch = Orchestrator(tmp_path)
    report = packs.TestReport(
        commands=["npm run build"],
        results=[packs.TestResult(command="npm run build", returncode=1, passed=False, stdout="", stderr="")],
        passed=False,
        blockers=[],
        pointers=[],
    )
    blocker = r"Error: No valid Tool binary found. Tried: node_modules\.bin\tool.exe and bin\tool.exe."
    change = orch._auto_code_change_for_test_failure(report=report, blocker_text=blocker)
    assert change is not None
    orch._materialize_code_change(change, actor_agent_id="coder_backend")

    patched = script_path.read_text(encoding="utf-8", errors="replace")
    assert "node_modules/.bin/tool.cmd" in patched
    assert "bin/tool.exe" in patched  # manual fallback must not be rewritten


def test_auto_fix_corrects_node_modules_bin_typo(tmp_path: Path, monkeypatch) -> None:
    if os.name != "nt":
        pytest.skip("Windows-specific npm shim behavior")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "node_modules" / ".bin").mkdir(parents=True, exist_ok=True)
    (tmp_path / "node_modules" / ".bin" / "tool.cmd").write_text("@echo off\r\necho ok\r\n", encoding="utf-8")

    script_path = tmp_path / "scripts" / "run-tool.js"
    script_path.write_text(
        "const bin = path.join(__dirname, '..', 'node_modules', 'bin', 'tool.exe');\n"
        "const manual = path.join(__dirname, '..', 'bin', 'tool.exe');\n"
        "console.log(bin, manual);\n",
        encoding="utf-8",
    )

    orch = Orchestrator(tmp_path)
    report = packs.TestReport(
        commands=["npm run build"],
        results=[packs.TestResult(command="npm run build", returncode=1, passed=False, stdout="", stderr="")],
        passed=False,
        blockers=[],
        pointers=[],
    )
    blocker = r"Error: No valid Tool binary found. Tried: node_modules\bin\tool.exe and bin\tool.exe."
    change = orch._auto_code_change_for_test_failure(report=report, blocker_text=blocker)
    assert change is not None
    orch._materialize_code_change(change, actor_agent_id="coder_backend")

    patched = script_path.read_text(encoding="utf-8", errors="replace")
    assert "node_modules" in patched
    assert "'.bin'" in patched or "\".bin\"" in patched
    assert "tool.cmd" in patched
    assert "bin/tool.exe" in patched or "bin', 'tool.exe" in patched
