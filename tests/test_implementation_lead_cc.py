from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app


def test_implementation_lead_is_cced_into_memory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    r2 = runner.invoke(app, ["task", "add", "hello"])
    assert r2.exit_code == 0, r2.output

    r3 = runner.invoke(app, ["run", "--mock", "--route", "L1"])
    assert r3.exit_code == 0, r3.output

    mem = tmp_path / ".vibe" / "views" / "implementation_lead" / "memory.jsonl"
    assert mem.exists()
    lines = [l.strip() for l in mem.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines, "expected CC memory records for implementation_lead"
    # At least one record should mention key workflow event types (patch/test/checkpoint).
    summaries: list[str] = []
    for ln in lines[-20:]:
        try:
            data = json.loads(ln)
        except json.JSONDecodeError:
            continue
        digest = data.get("digest") or {}
        summaries.append(str(digest.get("summary") or ""))
    joined = "\n".join(summaries)
    assert any(k in joined for k in ["PATCH_WRITTEN", "TEST_", "CHECKPOINT_CREATED", "PLAN_CREATED", "ROUTE_SELECTED"])

