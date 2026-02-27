from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel


class ArtifactPointer(BaseModel):
    path: str
    sha256: str
    size: int
    kind: Optional[str] = None

    def to_pointer(self) -> str:
        return f"{self.path}@sha256:{self.sha256}"


class ArtifactsStore:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.root = repo_root / ".vibe" / "artifacts" / "sha256"

    def _artifact_relpath(self, digest: str, suffix: str) -> Path:
        return Path("artifacts") / "sha256" / digest[:2] / f"{digest}{suffix}"

    def put_bytes(self, data: bytes, *, suffix: str = "", kind: Optional[str] = None) -> ArtifactPointer:
        digest = hashlib.sha256(data).hexdigest()
        rel = self._artifact_relpath(digest, suffix)
        abs_path = self.repo_root / ".vibe" / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        if not abs_path.exists():
            abs_path.write_bytes(data)
        return ArtifactPointer(path=str(Path(".vibe") / rel).replace("\\", "/"), sha256=digest, size=len(data), kind=kind)

    def put_text(self, text: str, *, suffix: str = ".txt", kind: Optional[str] = None) -> ArtifactPointer:
        return self.put_bytes(text.encode("utf-8"), suffix=suffix, kind=kind)

    def put_json(self, obj: Any, *, suffix: str = ".json", kind: Optional[str] = None) -> ArtifactPointer:
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        return self.put_bytes(data, suffix=suffix, kind=kind)

    def read_bytes(self, pointer: ArtifactPointer | str) -> bytes:
        if isinstance(pointer, str):
            path = pointer.split("@sha256:", 1)[0]
        else:
            path = pointer.path
        abs_path = self.repo_root / path
        return abs_path.read_bytes()

