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
