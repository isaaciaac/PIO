from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from vibe.storage.artifacts import ArtifactsStore, ArtifactPointer


class Checkpoint(BaseModel):
    id: str
    label: str
    created_at: str
    repo_ref: str
    ledger_offset: int
    artifacts: List[str] = Field(default_factory=list)
    green: bool = False
    restore_steps: List[str] = Field(default_factory=list)
    derived_from: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


class CheckpointsFile(BaseModel):
    version: int = 1
    checkpoints: List[Checkpoint] = Field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class CheckpointsStore:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.path = repo_root / ".vibe" / "checkpoints.json"
        self.artifacts = ArtifactsStore(repo_root)

    def load(self) -> CheckpointsFile:
        if not self.path.exists():
            return CheckpointsFile()
        raw = self.path.read_text(encoding="utf-8").strip()
        if not raw:
            return CheckpointsFile()
        data = json.loads(raw)
        if isinstance(data, list):
            return CheckpointsFile(checkpoints=[Checkpoint.model_validate(x) for x in data])
        return CheckpointsFile.model_validate(data)

    def save(self, file: CheckpointsFile) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(file.model_dump(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def list(self) -> List[Checkpoint]:
        return self.load().checkpoints

    def get(self, checkpoint_id: str) -> Checkpoint:
        for cp in self.list():
            if cp.id == checkpoint_id:
                return cp
        raise KeyError(f"checkpoint not found: {checkpoint_id}")

    def create(
        self,
        *,
        checkpoint_id: Optional[str] = None,
        label: str,
        repo_ref: str,
        ledger_offset: int,
        artifacts: Optional[List[str]] = None,
        green: bool,
        restore_steps: List[str],
        derived_from: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Checkpoint:
        file = self.load()
        cp = Checkpoint(
            id=checkpoint_id or f"ckpt_{uuid4().hex[:12]}",
            label=label,
            created_at=_now_iso(),
            repo_ref=repo_ref,
            ledger_offset=ledger_offset,
            artifacts=artifacts or [],
            green=green,
            restore_steps=restore_steps,
            derived_from=derived_from,
            meta=meta or {},
        )
        file.checkpoints.append(cp)
        self.save(file)
        return cp

    def snapshot_repo(self) -> ArtifactPointer:
        items: List[Dict[str, Any]] = []
        for path in self.repo_root.rglob("*"):
            if path.is_dir():
                continue
            rel = path.relative_to(self.repo_root)
            if rel.parts and rel.parts[0] in {".vibe", ".git"}:
                continue
            data = path.read_bytes()
            items.append(
                {
                    "path": rel.as_posix(),
                    "content_b64": base64.b64encode(data).decode("ascii"),
                }
            )
        payload = {"version": 1, "files": items}
        return self.artifacts.put_json(payload, suffix=".snapshot.json", kind="snapshot")

    def restore_snapshot(self, snapshot_pointer: ArtifactPointer | str) -> None:
        raw = self.artifacts.read_bytes(snapshot_pointer)
        payload = json.loads(raw.decode("utf-8"))
        files = payload.get("files", [])
        desired = {f["path"]: base64.b64decode(f["content_b64"].encode("ascii")) for f in files}

        # Write desired files
        for rel, data in desired.items():
            abs_path = self.repo_root / rel
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_bytes(data)

        # Remove extra files (best-effort)
        for path in self.repo_root.rglob("*"):
            if path.is_dir():
                continue
            rel = path.relative_to(self.repo_root)
            if rel.parts and rel.parts[0] in {".vibe", ".git"}:
                continue
            if rel.as_posix() not in desired:
                try:
                    path.unlink()
                except OSError:
                    pass
