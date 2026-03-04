from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app
from vibe.storage.artifacts import ArtifactsStore
from vibe.storage.checkpoints import CheckpointsStore


def test_mock_run_persists_intent_expansion_pack(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    r2 = runner.invoke(app, ["task", "add", "hello intent"])
    assert r2.exit_code == 0, r2.output

    r3 = runner.invoke(app, ["run", "--mock"])
    assert r3.exit_code == 0, r3.output
    ckpt_id = r3.output.strip()

    cps = CheckpointsStore(tmp_path)
    cp = cps.get(ckpt_id)
    ptr = str((cp.meta or {}).get("intent_ptr") or "").strip()
    assert ptr, "checkpoint meta should include intent_ptr"

    store = ArtifactsStore(tmp_path)
    raw = store.read_bytes(ptr).decode("utf-8", errors="replace")
    data = json.loads(raw)
    assert data.get("route_level") == "L1"
    assert isinstance(data.get("feature_backlog"), list)

