from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from vibe.cli import app
from vibe.orchestrator import Orchestrator, WriteScopeDeniedError
from vibe.schemas import packs


def test_write_scope_denies_out_of_scope_file_writes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    orch = Orchestrator(tmp_path)
    change = packs.CodeChange.model_validate(
        {
            "kind": "patch",
            "summary": "try write out of scope",
            "writes": [{"path": "README.md", "content": "nope\n"}],
            "files_changed": ["README.md"],
        }
    )

    with pytest.raises(WriteScopeDeniedError) as ei:
        orch._materialize_code_change(change, write_allowlist=["src/**"], write_denylist=[".vibe/**", ".git/**"])
    assert ei.value.path == "README.md"
    assert not (tmp_path / "README.md").exists()


def test_write_scope_denies_out_of_scope_copy_destinations(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    orch = Orchestrator(tmp_path)
    change = packs.CodeChange.model_validate(
        {
            "kind": "patch",
            "summary": "copy out of scope",
            "copies": [{"src": "vendor/tool.exe", "dst": "bin/tool.exe"}],
            "files_changed": ["bin/tool.exe"],
        }
    )

    with pytest.raises(WriteScopeDeniedError) as ei:
        orch._materialize_code_change(change, write_allowlist=["src/**"], write_denylist=[])
    assert ei.value.path == "bin/tool.exe"


def test_write_scope_denies_out_of_scope_patch_application(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    orch = Orchestrator(tmp_path)
    patch_text = """diff --git a/README.md b/README.md
index 0000000..1111111 100644
--- a/README.md
+++ b/README.md
@@ -0,0 +1 @@
+hi
"""
    ptr = orch.artifacts.put_text(patch_text, suffix=".patch", kind="patch").to_pointer()
    change = packs.CodeChange.model_validate({"kind": "patch", "summary": "apply patch out of scope", "patch_pointer": ptr})

    with pytest.raises(WriteScopeDeniedError) as ei:
        orch._materialize_code_change(change, write_allowlist=["src/**"], write_denylist=[])
    assert ei.value.path == "README.md"

