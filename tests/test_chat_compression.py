from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app


def test_cli_chat_auto_compresses_long_history_into_memory_and_artifacts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    os.environ["VIBE_MOCK_MODE"] = "1"
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    hist = tmp_path / ".vibe" / "views" / "pm" / "chat.jsonl"
    hist.parent.mkdir(parents=True, exist_ok=True)

    # Seed a long chat history: 32k+ chars in the last 16 messages.
    lines = []
    for i in range(40):
        role = "user" if i % 2 == 0 else "assistant"
        content = f"m{i} " + ("x" * 2000)
        lines.append({"ts": "2026-01-01T00:00:00Z", "role": role, "content": content})
    hist.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")

    mem = tmp_path / ".vibe" / "views" / "pm" / "memory.jsonl"
    if mem.exists():
        mem.write_text("", encoding="utf-8")

    r2 = runner.invoke(app, ["chat", "你好", "--json", "--history", "16"])
    assert r2.exit_code == 0, r2.output
    payload = json.loads(r2.output)
    assert payload.get("reply")

    assert mem.exists()
    mem_lines = [l for l in mem.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(mem_lines) >= 1
    rec = json.loads(mem_lines[-1])
    assert rec.get("agent_id") == "pm"
    pointers = rec.get("pointers") or []
    assert pointers

    # Archive artifacts should exist in workspace.
    for p in pointers[:2]:
        path = str(p).split("@sha256:", 1)[0]
        assert (tmp_path / path).exists()

    # Chat history should now start with a system digest line.
    hist_lines = [l for l in hist.read_text(encoding="utf-8").splitlines() if l.strip()]
    first = json.loads(hist_lines[0])
    assert first.get("role") == "system"

