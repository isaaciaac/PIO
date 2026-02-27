from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vibe.cli import app
from vibe.config import default_config, write_default_config
from vibe.repo import ensure_vibe_dirs
from vibe.storage.checkpoints import CheckpointsStore
from vibe.tools.git import GitTool


def _run(cmd: list[str], cwd: Path) -> str:
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, (cmd, r.stdout, r.stderr)
    return r.stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_branch_create_records_derived_from(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(["git", "init"], tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], tmp_path)
    _run(["git", "config", "user.name", "test"], tmp_path)
    (tmp_path / "a.txt").write_text("v1\n", encoding="utf-8")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "init"], tmp_path)

    ensure_vibe_dirs(tmp_path, agent_ids=list(default_config().agents.keys()))
    write_default_config(tmp_path, default_config())

    git = GitTool(tmp_path)
    head = git.head_sha()
    cps = CheckpointsStore(tmp_path)
    cps.create(
        checkpoint_id="ckpt1",
        label="x",
        repo_ref=head,
        ledger_offset=0,
        artifacts=[],
        green=True,
        restore_steps=[f"git checkout --detach {head}"],
    )

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    res = runner.invoke(app, ["branch", "create", "--from", "ckpt1", "--name", "test_branch"])
    assert res.exit_code == 0, res.output

    branch_json = tmp_path / ".vibe" / "branches" / "test_branch" / "branch.json"
    data = json.loads(branch_json.read_text(encoding="utf-8"))
    assert data["derived_from"] == "ckpt1"
    assert data["repo_ref"] == head

