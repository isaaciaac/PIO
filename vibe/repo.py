from __future__ import annotations

import sqlite3
from pathlib import Path


def find_repo_root(start: Path) -> Path:
    return start.resolve()


def ensure_vibe_dirs(repo_root: Path) -> None:
    vibe_dir = repo_root / ".vibe"
    (vibe_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (vibe_dir / "branches").mkdir(parents=True, exist_ok=True)
    (vibe_dir / "views").mkdir(parents=True, exist_ok=True)
    (vibe_dir / "manifests").mkdir(parents=True, exist_ok=True)
    (vibe_dir / "ledger.jsonl").touch(exist_ok=True)
    (vibe_dir / "checkpoints.json").write_text("[]\n", encoding="utf-8") if not (vibe_dir / "checkpoints.json").exists() else None

    # Create views for known agents if config exists later; init is minimal here.
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

