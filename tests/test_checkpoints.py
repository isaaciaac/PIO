from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from vibe.repo import ensure_vibe_dirs
from vibe.storage.checkpoints import CheckpointsStore
from vibe.tools.git import GitTool


def test_checkpoint_snapshot_restore_no_git(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("v1\n", encoding="utf-8")
    ensure_vibe_dirs(tmp_path, agent_ids=["router"])

    store = CheckpointsStore(tmp_path)
    snap = store.snapshot_repo()
    cp = store.create(
        checkpoint_id="ckpt_snap",
        label="snap",
        repo_ref="no-git",
        ledger_offset=0,
        artifacts=[snap.to_pointer()],
        green=True,
        restore_steps=["vibe checkpoint restore ckpt_snap"],
    )

    (tmp_path / "hello.txt").write_text("v2\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")

    store.restore_snapshot(cp.artifacts[0])
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "v1\n"
    assert not (tmp_path / "new.txt").exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_checkpoint_restore_git_detach_head(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=str(tmp_path), check=True, capture_output=True)

    (tmp_path / "a.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c1"], cwd=str(tmp_path), check=True, capture_output=True)

    git = GitTool(tmp_path)
    commit1 = git.head_sha()

    ensure_vibe_dirs(tmp_path, agent_ids=["router"])
    cps = CheckpointsStore(tmp_path)
    cps.create(
        checkpoint_id="ckpt_git",
        label="git",
        repo_ref=commit1,
        ledger_offset=0,
        artifacts=[],
        green=True,
        restore_steps=[f"git checkout --detach {commit1}"],
    )

    (tmp_path / "a.txt").write_text("v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c2"], cwd=str(tmp_path), check=True, capture_output=True)

    # Restore via the same logic as CLI would.
    git.checkout_detach(commit1)
    assert git.head_sha() == commit1

