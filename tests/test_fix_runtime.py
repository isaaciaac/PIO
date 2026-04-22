from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from vibe.orchestration.fix_runtime import FixRuntimeMixin
from vibe.orchestration.work_orders import ExecutionWorkOrder
from vibe.schemas import packs


class _DummyToolbox:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def run_cmd(self, *, agent_id: str, cmd: str, cwd: Path, timeout_s: int):
        self.calls.append((agent_id, cmd))
        return SimpleNamespace(stdout=f"stdout:{cmd}", stderr=f"stderr:{cmd}", meta=f"meta:{cmd}", returncode=0)


class _DummyFixRuntime(FixRuntimeMixin):
    def __init__(self) -> None:
        self.toolbox = _DummyToolbox()
        self.repo_root = Path(r"D:\R\HongyouCoding")
        self.branch_id = "main"
        self.install_state: list[str] = []
        self.logged_events: list[str] = []

    def _ensure_python_sandbox(self, *, agent_id: str) -> str:
        return "python-sandbox"

    def _rewrite_python_command(self, cmd: str, *, py: str) -> str:
        return f"{py}::{cmd}"

    def _record_python_install_state(self, *, command: str) -> None:
        self.install_state.append(command)

    def _env_remediation_commands_for_tests(self, *, report, blocker_text, error, envspec_commands):
        return ["python -m pip install -e .", "pytest -q"]

    def _is_env_fix_candidate(self, *, error, blocker_text: str) -> bool:
        return True

    def _append_guarded(self, *, event, activated_agents) -> None:
        self.logged_events.append(str(getattr(event, "type", "") or ""))


def test_delegated_fix_from_lead_work_order_runs_commands_once() -> None:
    runtime = _DummyFixRuntime()
    activated: list[tuple[str, str]] = []
    order = ExecutionWorkOrder(
        id="fix:env:1",
        kind="fix_work_order",
        owner="env_engineer",
        summary="Install missing deps",
        commands=["python -m pip install -e ."],
    )

    result = runtime._delegated_fix_from_lead_work_order(
        blocker_source="tests",
        failure_key="fp-1",
        lead_work_order=order,
        env_engineer_available=True,
        env_blocker=True,
        report=packs.TestReport(passed=False),
        blocker_text="ModuleNotFoundError",
        error=None,
        envspec_commands=[],
        executed_keys=set(),
        activate_agent=lambda agent_id, reason: activated.append((agent_id, reason)),
    )

    assert result is not None
    assert result.agent_id == "env_engineer"
    assert result.change.summary == "Install missing deps：已执行工单命令"
    assert result.commands == ["python-sandbox::python -m pip install -e ."]
    assert activated == [("env_engineer", "gate:lead_work_order")]
    assert runtime.install_state == ["python-sandbox::python -m pip install -e ."]


def test_delegated_env_fix_execution_logs_runtime_events() -> None:
    runtime = _DummyFixRuntime()
    activated: list[tuple[str, str]] = []

    result = runtime._delegated_env_fix_execution(
        blocker_source="tests",
        failure_key="fp-2",
        error=None,
        blocker_text="No module named x",
        report=packs.TestReport(passed=False),
        envspec_commands=[],
        env_remediation_keys=set(),
        activate_agent=lambda agent_id, reason: activated.append((agent_id, reason)),
        activated_agents={"router"},
        loop=2,
        lead_consult_ptrs=["artifact://hint"],
        route_level="L2",
        style="balanced",
    )

    assert result is not None
    assert result.agent_id == "env_engineer"
    assert result.change.summary.startswith("应用环境修复命令：")
    assert result.commands == ["python-sandbox::python -m pip install -e .", "python-sandbox::pytest -q"]
    assert activated == [("env_engineer", "gate:env_fix")]
    assert runtime.logged_events == ["STATE_TRANSITION", "ENV_UPDATED"]
