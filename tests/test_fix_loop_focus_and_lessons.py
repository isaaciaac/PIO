from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app
from vibe.orchestrator import Orchestrator
from vibe.schemas import packs
from vibe.schemas.memory import ChatDigest, MemoryRecord


def _mk_report(*results: packs.TestResult) -> packs.TestReport:
    cmds = [r.command for r in results]
    passed = all(r.passed for r in results)
    blockers: list[str] = []
    for r in results:
        if not r.passed:
            blockers.append(f"Command failed: {r.command}")
            break
    return packs.TestReport(commands=cmds, results=list(results), passed=passed, blockers=blockers, pointers=[])


def test_focus_cmd_for_pytest_collection_errors_uses_collect_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIBE_MOCK_MODE", raising=False)
    runner = CliRunner()
    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    orch = Orchestrator(tmp_path)
    report = _mk_report(
        packs.TestResult(command="python -m compileall .", returncode=0, passed=True, stdout="", stderr=""),
        packs.TestResult(command="pytest -q", returncode=1, passed=False, stdout="", stderr=""),
    )

    blocker_text = (
        "Command failed: pytest -q\n\n"
        "==================================== ERRORS ====================================\n"
        "ERROR tests/test_policy_engine.py\n"
        "ImportError while importing test module 'tests/test_policy_engine.py'.\n"
        "E   ImportError: cannot import name 'MockParser' from 'src.parsers.mock_parser'\n"
    )
    focus = orch._focus_commands_for_test_failure(report=report, blocker_text=blocker_text)
    assert len(focus) == 1
    assert "pytest" in focus[0]
    assert "--collect-only" in focus[0]
    assert "tests/test_policy_engine.py" in focus[0]


def test_extract_error_signals_picks_ts_and_missing_module_lines(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIBE_MOCK_MODE", raising=False)
    runner = CliRunner()
    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    orch = Orchestrator(tmp_path)
    text = (
        "src/pages/PostDetail.tsx(19,17): error TS2307: Cannot find module '../api/axios' or its corresponding type declarations.\n"
        "src/pages/CreatePost.tsx(16,16): error TS2339: Property 'token' does not exist on type 'User'.\n"
        "Cannot find module 'axios'\n"
    )
    signals = orch._extract_error_signals(text)
    assert any("error TS2307" in s for s in signals)
    assert any("error TS2339" in s for s in signals)
    assert any("Cannot find module" in s for s in signals)


def test_similar_lessons_returns_relevant_router_lesson(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIBE_MOCK_MODE", raising=False)
    runner = CliRunner()
    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    orch = Orchestrator(tmp_path)
    mem_path = tmp_path / ".vibe" / "views" / "router" / "memory.jsonl"
    mem_path.parent.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    rec = MemoryRecord(
        ts=ts,
        agent_id="router",
        kind="lesson",
        digest=ChatDigest(
            summary="Windows 下 npm scripts 的单引号不会被当作引号剥离，可能导致 ESLint glob 变成字面量而找不到文件。",
            pinned=["当 ESLint 报 No files matching the pattern \"'...\" 时，优先怀疑引号问题。"],
            background=[],
            open_questions=[],
        ),
        pointers=[".vibe/artifacts/sha256/xx/xxx.json@sha256:deadbeef"],
    )
    mem_path.write_text(rec.model_dump_json() + "\n", encoding="utf-8")

    hits = orch._similar_lessons_for_query(agent_id="router", query="ESLint No files matching the pattern \"'server/**/*.ts'\"", limit=1)
    assert hits
    assert "ESLint glob" in hits[0].digest.summary

