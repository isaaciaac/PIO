from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app
from vibe.orchestrator import Orchestrator


def test_hint_add_persists_and_injected_into_context(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    r2 = runner.invoke(app, ["task", "add", "实现一个最小可运行 demo"])
    assert r2.exit_code == 0, r2.output
    task_id = r2.output.strip()
    assert task_id.startswith("evt_")

    hint_text = "不要引入不存在的库或脚本；优先使用仓库里已有的 package.json scripts。"
    r3 = runner.invoke(app, ["hint", "add", hint_text, "--task", task_id])
    assert r3.exit_code == 0, r3.output
    hint_evt = r3.output.strip()
    assert hint_evt.startswith("evt_")

    orch = Orchestrator(tmp_path)
    task_evt = orch._find_task(task_id)
    ctx, _ = orch._build_context_packet(task_evt=task_evt)

    assert any("不要引入不存在的库" in c for c in ctx.constraints)
    assert any(str(p).startswith(".vibe/artifacts/") and "@sha256:" in str(p) for p in (ctx.log_pointers or []))

