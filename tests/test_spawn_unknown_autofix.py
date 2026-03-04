from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vibe.cli import app
from vibe.orchestrator import Orchestrator
from vibe.schemas import packs


def test_spawn_unknown_autofix_prefers_cmd_over_zero_byte_exe(tmp_path: Path, monkeypatch) -> None:
    if os.name != "nt":
        pytest.skip("Windows-specific shim behavior")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIBE_MOCK_MODE", raising=False)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    client = tmp_path / "client"
    (client / "scripts").mkdir(parents=True, exist_ok=True)
    (client / "package.json").write_text('{"name":"demo","scripts":{"build":"node scripts/run-tool.js"}}\n', encoding="utf-8")

    bin_dir = client / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "tool.exe").write_bytes(b"")  # zero-byte shim
    (bin_dir / "tool.cmd").write_text("@echo off\r\necho ok\r\n", encoding="utf-8")

    script_path = client / "scripts" / "run-tool.js"
    script_path.write_text("console.log('tool.exe');\n", encoding="utf-8")

    orch = Orchestrator(tmp_path)
    report = packs.TestReport(
        commands=['cd /d "client" && npm run build'],
        results=[
            packs.TestResult(
                command='cd /d "client" && npm run build',
                returncode=1,
                passed=False,
                stdout="",
                stderr="",
            )
        ],
        passed=False,
        blockers=[],
        pointers=[],
    )

    blocker_text = f"Error: spawn {str(bin_dir / 'tool.exe')} UNKNOWN"
    change = orch._auto_code_change_for_test_failure(report=report, blocker_text=blocker_text)
    assert change is not None
    assert change.kind == "patch"
    assert any(w.path.endswith("run-tool.js") for w in change.writes)
    patched = [w.content for w in change.writes if w.path.endswith("run-tool.js")][0]
    assert "tool.cmd" in patched

