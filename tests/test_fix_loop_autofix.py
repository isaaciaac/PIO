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


def test_auto_fix_eslint_windows_single_quote_glob(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output

    (tmp_path / "server" / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "server" / "src" / "index.ts").write_text("export const x = 1;\n", encoding="utf-8")

    pkg = {
        "name": "x",
        "private": True,
        "scripts": {"lint": "eslint 'server/**/*.ts' --fix"},
        "devDependencies": {"eslint": "^8.57.0"},
    }
    (tmp_path / "package.json").write_text(json.dumps(pkg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    orch = Orchestrator(tmp_path)
    report = _dummy_failed_report("npm run lint")
    blocker = (
        "Oops! Something went wrong! :(\n\n"
        "ESLint: 8.57.1\n\n"
        "No files matching the pattern \"'server/**/*.ts'\" were found.\n"
        "Please check for typing mistakes in the pattern.\n"
    )
    change = orch._auto_code_change_for_test_failure(report=report, blocker_text=blocker)
    assert change is not None

    orch._materialize_code_change(change, actor_agent_id="coder_backend")

    pkg2 = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    lint2 = str((pkg2.get("scripts") or {}).get("lint") or "")
    assert "'server/**/*.ts'" not in lint2
    assert '"server/**/*.ts"' in lint2


def test_auto_fix_node_missing_tsc_add_typescript(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output

    (tmp_path / "server").mkdir(parents=True, exist_ok=True)
    (tmp_path / "server" / "package.json").write_text(
        json.dumps(
            {
                "name": "server",
                "private": True,
                "scripts": {"build": "tsc"},
                "devDependencies": {},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    orch = Orchestrator(tmp_path)
    cmd = 'cd /d "server" && npm run build'
    report = _dummy_failed_report(cmd)
    blocker = (
        f"Command failed: {cmd}\n\n"
        "> build\n"
        "> tsc\n\n"
        "'tsc' is not recognized as an internal or external command,\n"
        "operable program or batch file.\n"
    )
    change = orch._auto_code_change_for_test_failure(report=report, blocker_text=blocker)
    assert change is not None
    assert any(w.path == "server/package.json" for w in change.writes)

    orch._materialize_code_change(change, actor_agent_id="coder_backend")
    pkg2 = json.loads((tmp_path / "server" / "package.json").read_text(encoding="utf-8"))
    dev2 = dict(pkg2.get("devDependencies") or {})
    assert "typescript" in dev2


def test_auto_fix_node_env_prefix_add_cross_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output

    (tmp_path / "server").mkdir(parents=True, exist_ok=True)
    (tmp_path / "server" / "package.json").write_text(
        json.dumps(
            {
                "name": "server",
                "private": True,
                "scripts": {"build": "NODE_ENV=production tsc"},
                "devDependencies": {"typescript": "^5.6.0"},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    orch = Orchestrator(tmp_path)
    cmd = 'cd /d "server" && npm run build'
    report = _dummy_failed_report(cmd)
    blocker = (
        f"Command failed: {cmd}\n\n"
        "> build\n"
        "> NODE_ENV=production tsc\n\n"
        "'NODE_ENV' is not recognized as an internal or external command,\n"
        "operable program or batch file.\n"
    )
    change = orch._auto_code_change_for_test_failure(report=report, blocker_text=blocker)
    assert change is not None

    orch._materialize_code_change(change, actor_agent_id="coder_backend")
    pkg2 = json.loads((tmp_path / "server" / "package.json").read_text(encoding="utf-8"))
    scripts2 = dict(pkg2.get("scripts") or {})
    assert scripts2.get("build", "").startswith("cross-env ")
    dev2 = dict(pkg2.get("devDependencies") or {})
    assert "cross-env" in dev2

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


def test_auto_fix_ts_auth_export_mismatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output

    (tmp_path / "server" / "src" / "routes").mkdir(parents=True, exist_ok=True)
    (tmp_path / "server" / "src" / "middleware").mkdir(parents=True, exist_ok=True)

    (tmp_path / "server" / "src" / "routes" / "customer.routes.ts").write_text(
        "import { auth } from '../middleware/auth';\nexport const x = auth;\n",
        encoding="utf-8",
    )
    (tmp_path / "server" / "src" / "middleware" / "auth.ts").write_text(
        "export const authenticate = () => {};\n",
        encoding="utf-8",
    )
    (tmp_path / "server" / "package.json").write_text(
        json.dumps({"name": "server", "private": True}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    orch = Orchestrator(tmp_path)
    report = _dummy_failed_report('cd /d "server" && npm run build')
    blocker = (
        "src/routes/customer.routes.ts(1,10): error TS2614: Module '\"../middleware/auth\"' has no exported member 'auth'.\n"
    )
    change = orch._auto_code_change_for_test_failure(report=report, blocker_text=blocker)
    assert change is not None
    assert len(change.writes) == 1
    assert change.writes[0].path == "server/src/middleware/auth.ts"

    orch._materialize_code_change(change, actor_agent_id="coder_backend")
    text = (tmp_path / "server" / "src" / "middleware" / "auth.ts").read_text(encoding="utf-8")
    assert "export const auth = authenticate" in text
