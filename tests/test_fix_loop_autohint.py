from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app
from vibe.orchestrator import Orchestrator
from vibe.schemas import packs


def test_fix_loop_autohint_db_mismatch_pool(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output

    (tmp_path / "backend" / "src" / "routes").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "src").mkdir(parents=True, exist_ok=True)

    (tmp_path / "backend" / "src" / "db.ts").write_text(
        "import { Pool } from 'pg';\nconst pool = new Pool({ connectionString: process.env.DATABASE_URL });\nexport default pool;\n",
        encoding="utf-8",
    )
    (tmp_path / "backend" / "src" / "routes" / "posts.ts").write_text(
        "import pool from '../db';\nexport async function x() { return pool.query('select 1'); }\n",
        encoding="utf-8",
    )

    orch = Orchestrator(tmp_path)
    cmd = 'cd /d \"backend\" && npm run build'
    report = packs.TestReport(
        commands=[cmd],
        results=[packs.TestResult(command=cmd, returncode=1, passed=False, stdout="", stderr="")],
        passed=False,
        blockers=[f"Command failed: {cmd}"],
        pointers=[],
    )
    blocker_text = (
        f"Command failed: {cmd}\n\n"
        "> build\n> tsc\n"
        "src/controllers/auth.ts(18,32): error TS2349: This expression is not callable.\n"
        "  Type 'Pool' has no call signatures.\n"
    )
    hint = orch._fix_loop_autohint_for_tests(report=report, blocker_text=blocker_text)
    assert "backend/src/db.ts#L1-L80@sha256:" in hint
    assert "backend/src/routes/posts.ts#L1-L140@sha256:" in hint
    assert "修复方向" in hint
