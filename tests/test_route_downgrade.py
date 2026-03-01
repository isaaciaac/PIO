from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app


def test_run_route_L3_downgrades_to_L2_in_mvp(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    os.environ["VIBE_MOCK_MODE"] = "1"
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    r2 = runner.invoke(app, ["task", "add", "test route downgrade"])
    assert r2.exit_code == 0, r2.output
    task_id = r2.output.strip().splitlines()[-1]
    assert task_id.startswith("evt_")

    r3 = runner.invoke(app, ["run", "--task", task_id, "--route", "L3", "--mock"])
    assert r3.exit_code == 0, r3.output

    ledger = (tmp_path / ".vibe" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in ledger if line.strip()]
    route_events = [e for e in events if e.get("type") == "ROUTE_SELECTED"]
    assert route_events, "expected ROUTE_SELECTED in ledger"
    last = route_events[-1]
    assert last.get("meta", {}).get("requested_route_level") == "L3"
    assert last.get("meta", {}).get("route_level") == "L2"

