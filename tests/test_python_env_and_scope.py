from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from vibe.cli import app
from vibe.orchestrator import Orchestrator
from vibe.schemas import packs
from vibe.schemas.events import new_event


def test_workspace_contract_includes_python_setup_commands(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    (tmp_path / "pyproject.toml").write_text(
        "[build-system]\nrequires = [\"setuptools>=61.0\"]\nbuild-backend = \"setuptools.build_meta\"\n"
        "[project]\nname = \"demo\"\nversion = \"0.1.0\"\ndependencies = [\"fastapi>=0.104.0\"]\n",
        encoding="utf-8",
    )

    orch = Orchestrator(tmp_path)
    _ptr, _summary, _excerpt = orch._write_workspace_contract(
        route_level="L1",
        style="balanced",
        tooling_ptr=None,
        tooling_available=["python", "pip"],
        tooling_missing=[],
    )
    contract = json.loads((tmp_path / ".vibe" / "manifests" / "workspace_contract.json").read_text(encoding="utf-8"))
    assert "python -m pip install -e ." in list((contract.get("commands") or {}).get("setup") or [])


def test_run_tests_preinstalls_python_dependencies(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    (tmp_path / "pyproject.toml").write_text(
        "[build-system]\nrequires = [\"setuptools>=61.0\"]\nbuild-backend = \"setuptools.build_meta\"\n"
        "[project]\nname = \"demo\"\nversion = \"0.1.0\"\ndependencies = [\"fastapi>=0.104.0\"]\n",
        encoding="utf-8",
    )

    orch = Orchestrator(tmp_path)
    calls: list[str] = []

    def fake_has_module(name: str) -> bool:
        return name != "fastapi"

    def fake_run_cmd(*, agent_id: str, cmd, cwd=None, timeout_s=None):
        command = cmd if isinstance(cmd, str) else " ".join(cmd)
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="stdout", stderr="", meta="")

    monkeypatch.setattr(orch, "_python_has_module", fake_has_module)
    monkeypatch.setattr(orch.toolbox, "run_cmd", fake_run_cmd)

    report = orch._run_tests(profile="unit", commands=["pytest -q"])
    assert report.passed is True
    assert calls[:2] == ["python -m pip install -e .", "pytest -q"]
    assert (tmp_path / ".vibe" / "manifests" / "python_env_state.json").exists()


def test_expand_fix_scope_for_missing_python_dependency_adds_manifests(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    (tmp_path / "pyproject.toml").write_text(
        "[build-system]\nrequires = [\"setuptools>=61.0\"]\nbuild-backend = \"setuptools.build_meta\"\n"
        "[project]\nname = \"demo\"\nversion = \"0.1.0\"\ndependencies = [\"fastapi>=0.104.0\"]\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("fastapi>=0.104.0\nsqlalchemy>=2.0.0\n", encoding="utf-8")
    (tmp_path / "src" / "core").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "core" / "parsers.py").write_text("def parse():\n    return {}\n", encoding="utf-8")

    orch = Orchestrator(tmp_path)
    allow, deny = orch._expand_fix_scope_for_blocker(
        allow=["src/core/parsers.py"],
        deny=[],
        blocker_text=(
            "ModuleNotFoundError: No module named 'sqlalchemy'\n"
            "ImportError: cannot import name 'MockParser' from 'src.core.parsers'"
        ),
    )
    assert "src/core/parsers.py" in allow
    assert "pyproject.toml" in allow
    assert "requirements.txt" in allow
    assert deny == []


def test_incident_for_tests_builds_error_object_for_missing_python_module(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    (tmp_path / "pyproject.toml").write_text(
        "[build-system]\nrequires = [\"setuptools>=61.0\"]\nbuild-backend = \"setuptools.build_meta\"\n"
        "[project]\nname = \"demo\"\nversion = \"0.1.0\"\ndependencies = [\"fastapi>=0.104.0\"]\n",
        encoding="utf-8",
    )

    orch = Orchestrator(tmp_path)
    blocker = "ModuleNotFoundError: No module named 'fastapi'"
    report = packs.TestReport(
        commands=["pytest -q"],
        results=[packs.TestResult(command="pytest -q", returncode=1, passed=False, stdout="", stderr=blocker)],
        passed=False,
        blockers=[blocker],
        pointers=[],
    )

    incident = orch._incident_for_tests(report=report, blocker_text=blocker, activated_agents={"router", "coder_backend", "qa", "env_engineer"})
    assert incident.error_object is not None
    assert incident.error_object.error_type == "config_missing"
    assert incident.error_object.failed_command == "pytest -q"
    assert "env" in incident.required_capabilities
    assert "deps" in incident.required_capabilities


def test_env_remediation_commands_for_missing_python_module_include_install(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    (tmp_path / "pyproject.toml").write_text(
        "[build-system]\nrequires = [\"setuptools>=61.0\"]\nbuild-backend = \"setuptools.build_meta\"\n"
        "[project]\nname = \"demo\"\nversion = \"0.1.0\"\ndependencies = [\"fastapi>=0.104.0\"]\n",
        encoding="utf-8",
    )

    orch = Orchestrator(tmp_path)
    blocker = "ModuleNotFoundError: No module named 'fastapi'"
    report = packs.TestReport(
        commands=["pytest -q"],
        results=[packs.TestResult(command="pytest -q", returncode=1, passed=False, stdout="", stderr=blocker)],
        passed=False,
        blockers=[blocker],
        pointers=[],
    )
    error = orch._diagnose_test_failure(report=report, blocker_text=blocker, observation={})
    cmds = orch._env_remediation_commands_for_tests(report=report, blocker_text=blocker, error=error, envspec_commands=[])
    assert cmds == ["python -m pip install -e ."]


def test_compile_preflight_commands_for_python_import_failure_include_compileall(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIBE_MOCK_MODE", raising=False)
    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    (tmp_path / "pyproject.toml").write_text(
        "[build-system]\nrequires = [\"setuptools>=61.0\"]\nbuild-backend = \"setuptools.build_meta\"\n"
        "[project]\nname = \"demo\"\nversion = \"0.1.0\"\ndependencies = [\"fastapi>=0.104.0\"]\n",
        encoding="utf-8",
    )

    orch = Orchestrator(tmp_path)
    blocker = "ImportError while importing test module tests/test_api.py\nModuleNotFoundError: No module named 'fastapi'"
    report = packs.TestReport(
        commands=["pytest -q"],
        results=[packs.TestResult(command="pytest -q", returncode=1, passed=False, stdout="", stderr=blocker)],
        passed=False,
        blockers=[blocker],
        pointers=[],
    )
    error = orch._diagnose_test_failure(report=report, blocker_text=blocker, observation={})
    cmds = orch._compile_preflight_commands_for_tests(
        report=report,
        blocker_text=blocker,
        error=error,
        focus_commands=["pytest -q --collect-only"],
    )
    assert "python -m compileall ." in cmds
    assert "pytest -q --collect-only" not in cmds


def test_lead_scope_expands_to_related_test_and_package_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    (tmp_path / "src" / "core").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "core" / "__init__.py").write_text("from .parsers import Parser\n", encoding="utf-8")
    (tmp_path / "src" / "core" / "parsers.py").write_text("class Parser:\n    pass\n", encoding="utf-8")
    (tmp_path / "tests" / "test_inference.py").write_text("from src.core import Parser\n", encoding="utf-8")

    orch = Orchestrator(tmp_path)
    error = packs.ErrorObject(
        error_type="missing_export",
        module="src.core.parsers",
        symbol="Parser",
        traceback_location="tests/test_inference.py:1",
        related_files=["tests/test_inference.py"],
        failed_command="pytest -q",
    )
    allow, deny, scope_level = orch._lead_work_order_scope(
        order=None,
        default_allow=["pyproject.toml"],
        default_deny=[],
        blocker_text="ImportError while importing test module tests/test_inference.py\ncannot import name 'Parser' from 'src.core.parsers'",
        error=error,
    )
    assert scope_level == "L2"
    assert "pyproject.toml" in allow
    assert "tests/test_inference.py" in allow
    assert "src/core/parsers.py" in allow
    assert "src/core/__init__.py" in allow
    assert deny == []


def test_source_candidates_for_test_path_maps_to_src_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "parser.py").write_text("class PolicyParser:\n    pass\n", encoding="utf-8")
    (tmp_path / "tests" / "test_parser.py").write_text("from src.parser import PolicyParser\n", encoding="utf-8")

    orch = Orchestrator(tmp_path)
    candidates = orch._source_candidates_for_test_path("tests/test_parser.py")
    assert "src/parser.py" in candidates


def test_recent_scope_mismatch_paths_are_reused_for_same_failure_fingerprint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    orch = Orchestrator(tmp_path)
    orch.ledger.append(
        new_event(
            agent="router",
            type="INCIDENT_CREATED",
            summary="Incident: 修复范围不匹配",
            branch_id="main",
            pointers=[],
            meta={
                "category": "scope_mismatch",
                "failure_fingerprint": "fp_same",
                "path": "src/parser.py",
                "allow": ["tests/test_parser.py"],
            },
        )
    )

    reused = orch._recent_scope_mismatch_paths(failure_fingerprint="fp_same")
    assert "src/parser.py" in reused
    assert "tests/test_parser.py" in reused


def test_diagnose_local_package_shadow_as_wrong_import_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\n", encoding="utf-8")
    (tmp_path / "app" / "__init__.py").write_text("from .app import app\n", encoding="utf-8")
    (tmp_path / "tests" / "test_integration.py").write_text("from app import app\n", encoding="utf-8")

    orch = Orchestrator(tmp_path)
    blocker = "ImportError while importing test module tests/test_integration.py\nModuleNotFoundError: No module named 'app.app'"
    report = packs.TestReport(
        commands=["pytest -q"],
        results=[packs.TestResult(command="pytest -q", returncode=1, passed=False, stdout="", stderr=blocker)],
        passed=False,
        blockers=[blocker],
        pointers=[],
    )
    observation, _ptr = orch._observe_test_failure(report=report, blocker_text=blocker)
    error = orch._diagnose_test_failure(report=report, blocker_text=blocker, observation=observation)

    assert error.error_type == "wrong_import_path"
    assert "py_package_shadow_root_module" in error.static_issue_ids
    assert "app.py" in list(observation.get("related_files") or [])
    assert orch._is_env_fix_candidate(error=error, blocker_text=blocker) is False


def test_static_python_skeleton_scanner_finds_export_and_signature_pitfalls(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    (tmp_path / "utils").mkdir(parents=True, exist_ok=True)
    (tmp_path / "models").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app.py").write_text(
        "from utils.reasoning_engine import ReasoningEngine\n"
        "from models.database import insert_rule, init_db\n\n"
        "def boot():\n"
        "    init_db('sqlite:///demo.db')\n"
        "    return insert_rule\n",
        encoding="utf-8",
    )
    (tmp_path / "utils" / "reasoning_engine.py").write_text(
        "class ReasoningResult:\n    pass\n\n"
        "def match_and_reason(text: str):\n    return ReasoningResult()\n",
        encoding="utf-8",
    )
    (tmp_path / "models" / "database.py").write_text(
        "def init_db():\n    return None\n\n"
        "def insert_policy_rule(data):\n    return data\n",
        encoding="utf-8",
    )

    orch = Orchestrator(tmp_path)
    issues = orch._python_static_skeleton_issues(
        observation={"module": "", "symbol": "", "related_files": ["app.py"]},
        blocker_text="ImportError while importing test module tests/test_integration.py",
    )
    issue_ids = {str(item.get("id") or "") for item in issues}

    assert "py_missing_local_export_symbol" in issue_ids
    assert "py_local_call_signature_mismatch" in issue_ids


def test_failure_signature_includes_static_issue_ids(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    orch = Orchestrator(tmp_path)
    report = packs.TestReport(
        commands=["pytest -q"],
        results=[packs.TestResult(command="pytest -q", returncode=1, passed=False, stdout="", stderr="ImportError")],
        passed=False,
        blockers=["ImportError"],
        pointers=[],
    )
    error = packs.ErrorObject(error_type="wrong_import_path", static_issue_ids=["py_package_shadow_root_module"])
    signature = orch._failure_signature(report=report, extracted=["ImportError"], blocker_text="ImportError", error=error)
    assert "static:py_package_shadow_root_module" in signature


def test_focus_commands_do_not_duplicate_collect_only_or_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    orch = Orchestrator(tmp_path)
    failed = "pytest -q --collect-only tests/test_integration.py"
    blocker = "ImportError while importing test module tests/test_integration.py\nModuleNotFoundError: No module named 'app.app'"
    report = packs.TestReport(
        commands=[failed],
        results=[packs.TestResult(command=failed, returncode=1, passed=False, stdout="", stderr=blocker)],
        passed=False,
        blockers=[blocker],
        pointers=[],
    )
    cmds = orch._focus_commands_for_test_failure(report=report, blocker_text=blocker)
    assert cmds == [failed]
