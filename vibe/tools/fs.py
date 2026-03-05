from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class FileReadResult:
    path: str
    start_line: int
    end_line: int
    sha256: str
    pointer: str
    content: str


class FsTool:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def read_file(self, path: str | Path, *, start_line: Optional[int] = None, end_line: Optional[int] = None) -> FileReadResult:
        abs_path = (self.repo_root / path).resolve()
        rel = abs_path.relative_to(self.repo_root).as_posix()
        text = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = start_line or 1
        end = end_line or len(text)
        chunk = "\n".join(text[start - 1 : end]) + ("\n" if end >= start else "")
        digest = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
        pointer = f"{rel}#L{start}-L{end}@sha256:{digest}"
        return FileReadResult(path=rel, start_line=start, end_line=end, sha256=digest, pointer=pointer, content=chunk)

    def write_file(self, path: str | Path, content: str) -> str:
        abs_path = (self.repo_root / path).resolve()
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        rel = abs_path.relative_to(self.repo_root).as_posix()
        return f"{rel}@sha256:{digest}"

    def copy_file(self, src: str | Path, dst: str | Path) -> str:
        src_abs = (self.repo_root / src).resolve()
        dst_abs = (self.repo_root / dst).resolve()
        dst_abs.parent.mkdir(parents=True, exist_ok=True)

        h = hashlib.sha256()
        with src_abs.open("rb") as r, dst_abs.open("wb") as w:
            shutil.copyfileobj(r, w, length=1024 * 1024)
        # Hash the destination (single source of truth)
        with dst_abs.open("rb") as rr:
            for chunk in iter(lambda: rr.read(1024 * 1024), b""):
                h.update(chunk)
        digest = h.hexdigest()
        rel = dst_abs.relative_to(self.repo_root).as_posix()
        return f"{rel}@sha256:{digest}"
