from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence, Set

from vibe.schemas.events import LedgerEvent


def ledger_path(repo_root: Path, branch_id: str = "main") -> Path:
    if branch_id == "main":
        return repo_root / ".vibe" / "ledger.jsonl"
    return repo_root / ".vibe" / "branches" / branch_id / "ledger.jsonl"


class Ledger:
    def __init__(self, repo_root: Path, *, branch_id: str = "main") -> None:
        self.repo_root = repo_root
        self.branch_id = branch_id
        self.path = ledger_path(repo_root, branch_id)

    def ensure(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def append(self, event: LedgerEvent) -> None:
        self.ensure()
        if not event.branch_id:
            event.branch_id = self.branch_id
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.model_dump(), ensure_ascii=False) + "\n")

    def iter_events(
        self,
        *,
        branch_id: Optional[str] = None,
        types: Optional[Set[str]] = None,
        limit: Optional[int] = None,
        reverse: bool = False,
    ) -> Iterator[LedgerEvent]:
        target = Ledger(self.repo_root, branch_id=branch_id or self.branch_id)
        target.ensure()
        lines = target.path.read_text(encoding="utf-8").splitlines()
        if reverse:
            lines = list(reversed(lines))
        count = 0
        for line in lines:
            if not line.strip():
                continue
            evt = LedgerEvent.model_validate_json(line)
            if types is not None and evt.type not in types:
                continue
            yield evt
            count += 1
            if limit is not None and count >= limit:
                return

    def last_event(self) -> Optional[LedgerEvent]:
        for evt in self.iter_events(limit=1, reverse=True):
            return evt
        return None

    def count_lines(self) -> int:
        self.ensure()
        with self.path.open("r", encoding="utf-8") as f:
            return sum(1 for _ in f)

