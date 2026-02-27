from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


@dataclass(frozen=True)
class RefItem:
    id: str
    title: str
    tags: str
    content: str
    source: str
    created_at: str
    updated_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class RefStore:
    def __init__(self, repo_root: Path) -> None:
        self.path = repo_root / ".vibe" / "refstore.sqlite"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
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
        return conn

    def upsert(self, *, id: str, title: str, tags: str = "", content: str, source: str = "") -> None:
        now = _now_iso()
        with self._connect() as conn:
            cur = conn.execute("SELECT created_at FROM ref_items WHERE id = ?", (id,))
            row = cur.fetchone()
            created_at = row[0] if row else now
            conn.execute(
                """
                INSERT INTO ref_items(id, title, tags, content, source, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  title=excluded.title,
                  tags=excluded.tags,
                  content=excluded.content,
                  source=excluded.source,
                  updated_at=excluded.updated_at
                """,
                (id, title, tags, content, source, created_at, now),
            )

    def get(self, id: str) -> Optional[RefItem]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT id, title, tags, content, source, created_at, updated_at FROM ref_items WHERE id = ?",
                (id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return RefItem(*row)

    def list(self) -> Iterable[RefItem]:
        with self._connect() as conn:
            cur = conn.execute("SELECT id, title, tags, content, source, created_at, updated_at FROM ref_items ORDER BY updated_at DESC")
            for row in cur.fetchall():
                yield RefItem(*row)

