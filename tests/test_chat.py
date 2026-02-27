from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app


def test_cli_chat_mock_returns_chatreply_and_persists_history(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    os.environ["VIBE_MOCK_MODE"] = "1"
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    r2 = runner.invoke(app, ["chat", "你好", "--json"])
    assert r2.exit_code == 0, r2.output
    payload = json.loads(r2.output)
    assert isinstance(payload.get("reply"), str)
    assert payload["reply"]

    hist = tmp_path / ".vibe" / "views" / "pm" / "chat.jsonl"
    assert hist.exists()
    lines = hist.read_text(encoding="utf-8").splitlines()
    # user + assistant
    assert len(lines) >= 2

