from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from vibe.schemas import packs
from vibe.schemas.events import new_event
from vibe.orchestration.work_orders import ExecutionWorkOrder


@dataclass(frozen=True)
class DelegatedFixExecution:
    agent_id: str
    change: packs.CodeChange
    pointers: list[str]
    commands: list[str]


class FixRuntimeMixin:
    def _execute_delegated_commands(
        self,
        *,
        agent_id: str,
        commands: list[str],
    ) -> tuple[list[str], bool, list[str]]:
        normalized = [str(x).strip() for x in list(commands or []) if str(x).strip()][:8]
        try:
            if any(re.search(r"\b(?:python|pip|pytest)\b", str(cmd).lower()) for cmd in normalized):
                py = self._ensure_python_sandbox(agent_id=agent_id)
                normalized = [self._rewrite_python_command(str(cmd), py=py) for cmd in normalized]
        except Exception:
            pass

        pointers: list[str] = []
        success = True
        for cmd in normalized:
            rr = self.toolbox.run_cmd(agent_id=agent_id, cmd=cmd, cwd=self.repo_root, timeout_s=3600)
            pointers.extend([rr.stdout, rr.stderr, rr.meta])
            if rr.returncode != 0:
                success = False
            try:
                low_cmd = str(cmd or "").lower()
                if rr.returncode == 0 and "-m pip install" in low_cmd:
                    self._record_python_install_state(command=cmd)
            except Exception:
                pass
        return normalized, success, pointers

    def _delegated_fix_from_lead_work_order(
        self,
        *,
        blocker_source: str,
        failure_key: str,
        lead_work_order: Optional[ExecutionWorkOrder],
        env_engineer_available: bool,
        env_blocker: bool,
        report: packs.TestReport,
        blocker_text: str,
        error: Optional[packs.ErrorObject],
        envspec_commands: list[str],
        executed_keys: set[str],
        activate_agent: Callable[..., None],
    ) -> Optional[DelegatedFixExecution]:
        if blocker_source != "tests" or lead_work_order is None:
            return None
        agent_id = str(getattr(lead_work_order, "owner", "") or "").strip()
        if agent_id not in {"env_engineer", "ops_engineer"}:
            return None

        commands = [str(x).strip() for x in list(getattr(lead_work_order, "commands", []) or []) if str(x).strip()][:8]
        if agent_id == "env_engineer" and not commands and env_engineer_available and env_blocker:
            try:
                commands = self._env_remediation_commands_for_tests(
                    report=report,
                    blocker_text=blocker_text,
                    error=error,
                    envspec_commands=envspec_commands,
                )
            except Exception:
                commands = []

        execution_key = "|".join(
            [
                failure_key,
                agent_id,
                str(getattr(lead_work_order, "summary", "") or "").strip()[:120],
                *commands[:4],
            ]
        )
        if not commands or execution_key in executed_keys:
            return None

        executed_keys.add(execution_key)
        activate_agent(agent_id, reason="gate:lead_work_order")
        commands, success, pointers = self._execute_delegated_commands(agent_id=agent_id, commands=commands)
        summary = str(getattr(lead_work_order, "summary", "") or "").strip()
        change = packs.CodeChange(
            kind="noop",
            summary=(f"{summary}：已执行工单命令" if success else f"{summary}：已执行工单命令但仍失败"),
            files_changed=[],
        )
        return DelegatedFixExecution(
            agent_id=agent_id,
            change=change,
            pointers=pointers,
            commands=commands,
        )

    def _delegated_env_fix_execution(
        self,
        *,
        blocker_source: str,
        failure_key: str,
        error: Optional[packs.ErrorObject],
        blocker_text: str,
        report: packs.TestReport,
        envspec_commands: list[str],
        env_remediation_keys: set[str],
        activate_agent: Callable[..., None],
        activated_agents: set[str],
        loop: int,
        lead_consult_ptrs: list[str],
        route_level: str,
        style: str,
    ) -> Optional[DelegatedFixExecution]:
        if blocker_source != "tests" or not self._is_env_fix_candidate(error=error, blocker_text=blocker_text):
            return None
        try:
            commands = self._env_remediation_commands_for_tests(
                report=report,
                blocker_text=blocker_text,
                error=error,
                envspec_commands=envspec_commands,
            )
        except Exception:
            commands = []

        env_key = "|".join([failure_key, *commands[:4]])
        if not commands or env_key in env_remediation_keys:
            return None

        env_remediation_keys.add(env_key)
        activate_agent("env_engineer", reason="gate:env_fix")
        self._append_guarded(
            event=new_event(
                agent="router",
                type="STATE_TRANSITION",
                summary=f"Fix-loop {loop}: env remediation env_engineer",
                branch_id=self.branch_id,
                pointers=list(lead_consult_ptrs or []),
                meta={
                    "phase": "fix_loop",
                    "loop": loop,
                    "blocker_source": blocker_source,
                    "fix_agent": "env_engineer",
                    "commands": commands,
                    "route_level": route_level,
                    "style": style,
                },
            ),
            activated_agents=activated_agents,
        )
        commands, success, pointers = self._execute_delegated_commands(agent_id="env_engineer", commands=commands)
        summary = " / ".join(commands[:2]).strip()
        if len(commands) > 2:
            summary = f"{summary} …"
        change = packs.CodeChange(
            kind="noop",
            summary=(f"应用环境修复命令：{summary}" if success else f"尝试环境修复命令但仍失败：{summary}"),
            files_changed=[],
        )
        self._append_guarded(
            event=new_event(
                agent="env_engineer",
                type="ENV_UPDATED",
                summary=change.summary,
                branch_id=self.branch_id,
                pointers=pointers,
                meta={
                    "loop": loop,
                    "commands": commands,
                    "passed": success,
                    "route_level": route_level,
                    "style": style,
                },
            ),
            activated_agents=activated_agents,
        )
        return DelegatedFixExecution(
            agent_id="env_engineer",
            change=change,
            pointers=pointers,
            commands=commands,
        )
