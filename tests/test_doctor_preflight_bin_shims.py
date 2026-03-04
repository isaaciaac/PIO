from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vibe.cli import app
from vibe.orchestrator import Orchestrator
from vibe.storage.artifacts import ArtifactsStore


def test_doctor_preflight_reports_zero_byte_bin_exe_and_bin_usage(tmp_path: Path, monkeypatch) -> None:
    if os.name != "nt":
        pytest.skip("Windows-specific shim behavior")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIBE_MOCK_MODE", raising=False)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    client = tmp_path / "client"
    (client / "scripts").mkdir(parents=True, exist_ok=True)
    (client / "package.json").write_text(
        json.dumps({"name": "demo", "scripts": {"build": "node scripts/run-tool.js"}}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    bin_dir = client / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "tool.exe").write_bytes(b"")  # zero-byte shim
    (bin_dir / "tool.cmd").write_text("@echo off\r\necho ok\r\n", encoding="utf-8")

    (client / "scripts" / "run-tool.js").write_text(
        "const p = 'node_modules/.bin/tool.exe';\nconsole.log(p);\n",
        encoding="utf-8",
    )

    orch = Orchestrator(tmp_path)
    ptr, _summary = orch._doctor_preflight(max_findings=6)
    assert ptr, "expected a doctor report pointer"

    store = ArtifactsStore(tmp_path)
    raw = store.read_bytes(ptr)
    report = json.loads(raw.decode("utf-8", errors="replace"))
    findings = list(report.get("findings") or [])
    kinds = {str(f.get("kind") or "") for f in findings}
    assert "node_bin_zero_byte_exe" in kinds
    assert "script_uses_node_modules_bin" in kinds

