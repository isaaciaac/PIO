from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app
from vibe.orchestrator import Orchestrator
from vibe.schemas import packs


def test_repo_excerpts_include_db_ts_for_pool_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output

    # Minimal backend layout
    (tmp_path / "backend" / "src" / "controllers").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "src" / "routes").mkdir(parents=True, exist_ok=True)

    (tmp_path / "backend" / "src" / "db.ts").write_text(
        "import { Pool } from 'pg';\nconst pool = new Pool();\nexport default pool;\n",
        encoding="utf-8",
    )
    (tmp_path / "backend" / "src" / "routes" / "posts.ts").write_text(
        "import pool from '../db';\nexport default pool;\n",
        encoding="utf-8",
    )
    (tmp_path / "backend" / "src" / "controllers" / "auth.ts").write_text(
        "import db from '../db';\nexport async function x() { await (db as any)('users'); }\n",
        encoding="utf-8",
    )

    orch = Orchestrator(tmp_path)
    stdout_ptr = orch.artifacts.put_text(
        "\n".join(
            [
                "> blog-backend@1.0.0 build",
                "> tsc",
                "src/controllers/auth.ts(2,33): error TS2349: This expression is not callable.",
                "  Type 'Pool' has no call signatures.",
            ]
        )
        + "\n",
        suffix=".stdout.txt",
        kind="cmd",
    ).to_pointer()
    stderr_ptr = orch.artifacts.put_text("", suffix=".stderr.txt", kind="cmd").to_pointer()

    cmd = 'cd /d \"backend\" && npm run build'
    report = packs.TestReport(
        commands=[cmd],
        results=[
            packs.TestResult(command=cmd, returncode=1, passed=False, stdout=stdout_ptr, stderr=stderr_ptr),
        ],
        passed=False,
        blockers=[f"Command failed: {cmd}"],
        pointers=[stdout_ptr, stderr_ptr],
    )

    blob = orch._repo_excerpts_for_test_failure(report)
    assert "backend/src/db.ts#L1-L200@sha256:" in blob
    assert "backend/src/routes/posts.ts#L1-L220@sha256:" in blob
