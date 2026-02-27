from __future__ import annotations

from pathlib import Path

from vibe.repo import ensure_vibe_dirs
from vibe.storage.artifacts import ArtifactsStore


def test_artifacts_dedup_by_sha256(tmp_path: Path) -> None:
    ensure_vibe_dirs(tmp_path, agent_ids=["router"])
    store = ArtifactsStore(tmp_path)

    p1 = store.put_bytes(b"hello", suffix=".txt", kind="test")
    p2 = store.put_bytes(b"hello", suffix=".txt", kind="test")

    assert p1.sha256 == p2.sha256
    assert p1.path == p2.path
    assert (tmp_path / p1.path).read_bytes() == b"hello"

