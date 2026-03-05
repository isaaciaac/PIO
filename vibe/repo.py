from __future__ import annotations

import sqlite3
from pathlib import Path

from vibe.storage.checkpoints import CheckpointsFile


def find_repo_root(start: Path) -> Path:
    path = start.resolve()
    if path.is_file():
        path = path.parent
    cur = path
    while True:
        if (cur / ".git").exists():
            return cur
        if (cur / ".vibe").exists():
            return cur
        parent = cur.parent
        if parent == cur:
            return path
        cur = parent


def ensure_vibe_dirs(repo_root: Path, *, agent_ids: list[str] | None = None) -> None:
    vibe_dir = repo_root / ".vibe"
    (vibe_dir / "artifacts" / "sha256").mkdir(parents=True, exist_ok=True)
    (vibe_dir / "branches").mkdir(parents=True, exist_ok=True)
    (vibe_dir / "views").mkdir(parents=True, exist_ok=True)
    (vibe_dir / "manifests").mkdir(parents=True, exist_ok=True)
    (vibe_dir / "knowledge").mkdir(parents=True, exist_ok=True)
    (vibe_dir / "ledger.jsonl").touch(exist_ok=True)
    checkpoints_path = vibe_dir / "checkpoints.json"
    if not checkpoints_path.exists():
        checkpoints_path.write_text(CheckpointsFile().model_dump_json(indent=2) + "\n", encoding="utf-8")

    for manifest in (vibe_dir / "manifests" / "project_manifest.md", vibe_dir / "manifests" / "run_manifest.md"):
        if not manifest.exists():
            manifest.write_text("", encoding="utf-8")

    kb_path = vibe_dir / "knowledge" / "solutions.yaml"
    if not kb_path.exists():
        kb_path.write_text(
            "# Workspace-local knowledge entries.\n"
            "#\n"
            "# This file is optional. If present, entries here override built-in knowledge by `id`.\n"
            "# Keep entries short and evidence-oriented.\n"
            "version: 1\n"
            "entries: []\n",
            encoding="utf-8",
        )

    if agent_ids:
        for agent_id in agent_ids:
            view_dir = vibe_dir / "views" / agent_id
            view_dir.mkdir(parents=True, exist_ok=True)
            (view_dir / "memory.jsonl").touch(exist_ok=True)
            if not (view_dir / "bookmarks.json").exists():
                (view_dir / "bookmarks.json").write_text("[]\n", encoding="utf-8")
            if not (view_dir / "rollbacks.md").exists():
                (view_dir / "rollbacks.md").write_text("# Rollback playbook\n\n", encoding="utf-8")
            if not (view_dir / "notes.md").exists():
                (view_dir / "notes.md").write_text("", encoding="utf-8")

    # Reference store
    ref_db = vibe_dir / "refstore.sqlite"
    if not ref_db.exists():
        conn = sqlite3.connect(ref_db)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ref_items(
                  id TEXT PRIMARY KEY,
                  title TEXT NOT NULL,
                  tags TEXT,
                  content TEXT NOT NULL,
                  source TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
