from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app
from vibe.orchestrator import Orchestrator
from vibe.schemas import packs


def _dummy_failed_report(cmd: str) -> packs.TestReport:
    return packs.TestReport(
        commands=[cmd],
        results=[packs.TestResult(command=cmd, returncode=1, passed=False, stdout="", stderr="")],
        passed=False,
        blockers=[f"Command failed: {cmd}"],
        pointers=[],
    )


def test_auto_fix_eslint_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output

    pkg = {
        "name": "x",
        "private": True,
        "scripts": {"lint": "eslint . --ext .ts,.tsx"},
        "devDependencies": {"eslint": "^8.57.0"},
    }
    (tmp_path / "package.json").write_text(json.dumps(pkg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    orch = Orchestrator(tmp_path)
    report = _dummy_failed_report("npm run lint")
    blocker = "Oops! Something went wrong! :(\n\nESLint couldn't find a configuration file."
    change = orch._auto_code_change_for_test_failure(report=report, blocker_text=blocker)
    assert change is not None
    assert {w.path for w in change.writes} >= {".eslintrc.cjs", "package.json"}

    orch._materialize_code_change(change, actor_agent_id="coder_backend")

    assert (tmp_path / ".eslintrc.cjs").exists()
    pkg2 = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    assert "@typescript-eslint/parser" in (pkg2.get("devDependencies") or {})
    assert "@typescript-eslint/eslint-plugin" in (pkg2.get("devDependencies") or {})


def test_auto_fix_vite_index_html(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output

    (tmp_path / "client" / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "client" / "src" / "main.tsx").write_text("console.log('hi')\n", encoding="utf-8")
    pkg = {
        "name": "client",
        "private": True,
        "scripts": {"build": "vite build"},
        "devDependencies": {"vite": "^5.0.0"},
    }
    (tmp_path / "client" / "package.json").write_text(json.dumps(pkg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    orch = Orchestrator(tmp_path)
    report = _dummy_failed_report('cd /d "client" && npm run build')
    blocker = 'error during build:\nCould not resolve entry module "index.html".'
    change = orch._auto_code_change_for_test_failure(report=report, blocker_text=blocker)
    assert change is not None
    assert len(change.writes) == 1
    assert change.writes[0].path == "client/index.html"

    orch._materialize_code_change(change, actor_agent_id="coder_backend")
    assert (tmp_path / "client" / "index.html").exists()

