from __future__ import annotations

import os
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set, Tuple
from uuid import uuid4

from vibe.agents.registry import AGENT_REGISTRY
from vibe.config import VibeConfig
from vibe.policy import ToolPolicy, resolve_policy_mode
from vibe.schemas import packs
from vibe.schemas.events import LedgerEvent, new_event
from vibe.storage.artifacts import ArtifactsStore
from vibe.storage.checkpoints import CheckpointsStore
from vibe.storage.ledger import Ledger
from vibe.storage.ledger import ledger_path
from vibe.toolbox import Toolbox
from vibe.routes import DiffStats, decide_route
from vibe.context import effective_context_config, read_memory_records
from vibe.style import normalize_style, style_workflow_hint


@dataclass(frozen=True)
class RunResult:
    checkpoint_id: str
    green: bool


class Orchestrator:
    def __init__(self, repo_root: Path, *, policy_mode: Optional[str] = None) -> None:
        self.repo_root = repo_root
        cfg_path = repo_root / ".vibe" / "vibe.yaml"
        if not cfg_path.exists():
            raise FileNotFoundError("Missing .vibe/vibe.yaml. Run `vibe init` first.")
        self.config = VibeConfig.load(cfg_path)

        self.policy = ToolPolicy(mode=resolve_policy_mode(self.config.policy.mode, override=policy_mode))
        self.toolbox = Toolbox(repo_root, config=self.config, policy=self.policy)

        self.branch_id = self._detect_branch_id()
        self.ledger = Ledger(repo_root, branch_id=self.branch_id)
        self.main_ledger = Ledger(repo_root, branch_id="main")
        self.artifacts = ArtifactsStore(repo_root)
        self.checkpoints = CheckpointsStore(repo_root)

    def _detect_branch_id(self) -> str:
        if self.policy.mode == "chat_only":
            return "main"
        try:
            branch = self.toolbox.git_current_branch(agent_id="router")
        except Exception:
            return "main"
        if branch in {"main", "master"}:
            return "main"
        if branch == "HEAD":
            return "main"
        if ledger_path(self.repo_root, branch).exists():
            return branch
        return "main"

    def _agent(self, agent_id: str):
        cls = AGENT_REGISTRY.get(agent_id)
        if not cls:
            raise KeyError(f"Unknown agent id: {agent_id}")
        cfg = self.config.agents[agent_id]
        return cls(cfg, providers=self.config.providers)

    def _find_task(self, task_id: Optional[str]) -> LedgerEvent:
        if task_id:
            for evt in self.ledger.iter_events():
                if evt.id == task_id:
                    return evt
            for evt in self.main_ledger.iter_events():
                if evt.id == task_id:
                    return evt
            raise KeyError(f"task not found: {task_id}")

        for evt in self.ledger.iter_events(types={"REQ_CREATED"}, reverse=True):
            return evt
        for evt in self.main_ledger.iter_events(types={"REQ_CREATED"}, reverse=True):
            return evt
        raise RuntimeError("No tasks found. Use `vibe task add \"...\"` first.")

    def _build_context_packet(self) -> packs.ContextPacket:
        pointers: List[str] = []
        for rel in [".vibe/manifests/project_manifest.md", ".vibe/manifests/run_manifest.md"]:
            path = self.repo_root / rel
            if path.exists():
                pointers.append(self.toolbox.read_file(agent_id="router", path=rel, start_line=1, end_line=200).pointer)
        recent = []
        for evt in self.ledger.iter_events(limit=20, reverse=True):
            recent.append(packs.ContextEventRef(id=evt.id, summary=evt.summary, pointers=evt.pointers))
        return packs.ContextPacket(repo_pointers=pointers, recent_events=recent)

    def _recent_test_fail_count(self, *, lookback_events: int = 50) -> int:
        count = 0
        for evt in self.ledger.iter_events(limit=lookback_events, reverse=True):
            if evt.type == "TEST_FAILED":
                count += 1
        return count

    def _git_diff_stats_best_effort(self) -> DiffStats:
        try:
            return self.toolbox.git_diff_stats(agent_id="router")
        except Exception:
            return DiffStats()

    def _agents_for_route(self, route_level: packs.RouteLevel) -> list[str]:
        profile = (self.config.routes.levels or {}).get(route_level)
        agents = list(profile.agents) if profile else []
        if not agents:
            # Backward compatible fallback: treat as L1 minimal set.
            agents = ["pm", "router", "coder_backend", "qa"] if route_level != "L0" else ["router", "coder_backend", "qa"]
        # Router is mandatory for ledger/gates.
        if "router" not in agents:
            agents = ["router", *agents]
        # De-duplicate while preserving order.
        out: list[str] = []
        seen: set[str] = set()
        for a in agents:
            a = str(a).strip()
            if not a or a in seen:
                continue
            seen.add(a)
            out.append(a)
        return out

    def _append_guarded(self, *, event: LedgerEvent, activated_agents: Set[str]) -> None:
        if event.agent != "user" and event.agent not in activated_agents:
            raise RuntimeError(f"Agent not activated for this route: {event.agent}")
        agent_cfg = self.config.agents.get(event.agent)
        if agent_cfg and agent_cfg.memory_scope.ledger_write_types:
            allowed = set(agent_cfg.memory_scope.ledger_write_types)
            if event.type not in allowed:
                raise RuntimeError(f"Ledger write type not allowed for agent {event.agent}: {event.type}")
        self.ledger.append(event)

    def _agent_memory_system(self, agent_id: str) -> Optional[str]:
        view_dir = self.repo_root / ".vibe" / "views" / agent_id
        mem_path = view_dir / "memory.jsonl"
        if not mem_path.exists():
            return None
        ctx_cfg = effective_context_config(self.config, agent_id=agent_id)
        limit = max(0, min(int(ctx_cfg.keep_last_digests) * 3, 24))
        recs = read_memory_records(mem_path, limit=limit)
        if not recs:
            return None
        take = recs[-max(1, int(ctx_cfg.keep_last_digests)) :]
        lines: list[str] = []
        lines.append("以下是该工种的结构化记忆摘要（来自 .vibe/views/<agent>/memory.jsonl；事实以 pointers 展开为准）：")
        for r in take:
            pinned = [s for s in (r.digest.pinned or [])][:3]
            pin = ("；".join(pinned))[:240] if pinned else ""
            ptrs = ", ".join(list(r.pointers or [])[:2])
            lines.append(f"- {r.digest.summary.strip()[:200]}")
            if pin:
                lines.append(f"  要点: {pin}")
            if ptrs:
                lines.append(f"  pointers: {ptrs}")
        return "\n".join(lines).strip()

    def _messages_with_memory(self, *, agent_id: str, system: str, user: str) -> list[dict[str, str]]:
        msgs: list[dict[str, str]] = [{"role": "system", "content": system}]
        mem_text = self._agent_memory_system(agent_id)
        if mem_text:
            msgs.append({"role": "system", "content": mem_text})
        msgs.append({"role": "user", "content": user})
        return msgs

    def _python_has_module(self, name: str) -> bool:
        try:
            return importlib.util.find_spec(name) is not None
        except Exception:
            return False

    def _materialize_code_change(self, change: packs.CodeChange) -> Tuple[packs.CodeChange, List[str]]:
        write_pointers: List[str] = []
        if change.writes:
            for w in change.writes:
                rel = (w.path or "").replace("\\", "/").lstrip("/")
                if rel.startswith(".vibe/") or rel.startswith(".git/"):
                    raise RuntimeError(f"Refusing to write internal path: {w.path}")
                ptr = self.toolbox.write_file(agent_id="coder_backend", path=w.path, content=w.content)
                write_pointers.append(ptr)

            if not change.files_changed:
                change.files_changed = [w.path for w in change.writes if w.path]

            # Best-effort patch evidence.
            patch_ptr: Optional[str] = None
            try:
                if self.toolbox.git_is_repo(agent_id="router"):
                    diff = self.toolbox.git_diff(agent_id="router")
                    patch_ptr = diff.stdout
            except Exception:
                patch_ptr = None

            if not patch_ptr:
                patch_ptr = self.artifacts.put_json(
                    {
                        "kind": "writes",
                        "files": [{"path": w.path, "pointer": p} for w, p in zip(change.writes, write_pointers)],
                    },
                    suffix=".writes.json",
                    kind="patch",
                ).to_pointer()

            change.kind = "patch"
            change.patch_pointer = patch_ptr

        # If the model returned inline patch text, store it into artifacts.
        if change.patch_pointer and "@sha256:" not in change.patch_pointer:
            change.patch_pointer = self.artifacts.put_text(
                change.patch_pointer,
                suffix=".patch.diff",
                kind="patch",
            ).to_pointer()

        if change.kind == "noop" or (not change.patch_pointer and not change.commit_hash and not write_pointers):
            patch_ptr = self.artifacts.put_text("mock: no code changes", suffix=".patch.txt", kind="patch").to_pointer()
            change = packs.CodeChange(kind="patch", summary=change.summary or "mock patch", patch_pointer=patch_ptr, files_changed=change.files_changed)

        return change, write_pointers

    def _run_tests(self, *, profile: str) -> packs.TestReport:
        if os.getenv("VIBE_MOCK_MODE", "").strip() == "1":
            return packs.TestReport(
                commands=["mock"],
                results=[packs.TestResult(command="mock", returncode=0, passed=True, stdout="", stderr="")],
                passed=True,
                blockers=[],
                pointers=[],
            )

        commands: List[str] = []

        is_py = (self.repo_root / "pyproject.toml").exists() or (self.repo_root / "tests").exists()
        is_node = (self.repo_root / "package.json").exists()
        tests_dir = self.repo_root / "tests"
        has_py_tests = False
        if tests_dir.exists():
            try:
                has_py_tests = any(p.is_file() for p in tests_dir.rglob("test*.py"))
            except Exception:
                has_py_tests = True

        p = profile.strip().lower()
        if p == "smoke":
            if is_py:
                commands = ["python -m compileall ."]
            elif is_node:
                commands = ["npm test"]
            else:
                return packs.TestReport(commands=[], results=[], passed=True, blockers=[], pointers=[])
        else:
            if is_py:
                # Prefer pytest when available and the project appears to have tests.
                if has_py_tests and self._python_has_module("pytest"):
                    commands = ["python -m compileall .", "pytest -q"]
                else:
                    commands = ["python -m compileall .", "python -m unittest -q"]
            elif is_node:
                commands = ["npm test"]
            else:
                return packs.TestReport(commands=[], results=[], passed=True, blockers=[], pointers=[])

        results: List[packs.TestResult] = []
        blockers: List[str] = []
        pointers: List[str] = []
        for cmd in commands:
            r = self.toolbox.run_cmd(agent_id="qa", cmd=cmd, cwd=self.repo_root, timeout_s=1800)
            passed = r.returncode == 0
            results.append(packs.TestResult(command=cmd, returncode=r.returncode, passed=passed, stdout=r.stdout, stderr=r.stderr, meta=r.meta))
            pointers.extend([r.stdout, r.stderr, r.meta])
            if not passed:
                blockers.append(f"Command failed: {cmd}")

        return packs.TestReport(commands=commands, results=results, passed=all(x.passed for x in results), blockers=blockers, pointers=pointers)

    def run(self, *, task_id: Optional[str] = None, route: Optional[str] = None, style: Optional[str] = None) -> RunResult:
        task_evt = self._find_task(task_id)
        task_text = str(task_evt.meta.get("text") or task_evt.summary)

        resolved_style = normalize_style(style or os.getenv("VIBE_STYLE") or getattr(self.config.behavior, "style", "balanced"))
        workflow_hint = style_workflow_hint(resolved_style)

        diff = self._git_diff_stats_best_effort()
        decision = decide_route(
            task_text=task_text,
            diff=diff,
            recent_test_fail_count=self._recent_test_fail_count(),
            requested_level=route,
        )
        route_level = decision.route_level
        activated_agents_list = self._agents_for_route(route_level)
        activated_agents: Set[str] = set(activated_agents_list)

        # Ledger: route selection + activation set (must be auditable).
        route_pointers = [p for p in [diff.pointer] if p]
        self._append_guarded(
            event=new_event(
                agent="router",
                type="ROUTE_SELECTED",
                summary=f"Selected route {route_level}",
                branch_id=self.branch_id,
                pointers=route_pointers,
                meta={
                    "route_level": route_level,
                    "reasons": decision.reasons,
                    "style": resolved_style,
                    "diff": {
                        "files": diff.file_count,
                        "loc_added": diff.loc_added,
                        "loc_deleted": diff.loc_deleted,
                        "loc_changed": diff.loc_changed,
                    },
                },
            ),
            activated_agents=activated_agents,
        )
        self._append_guarded(
            event=new_event(
                agent="router",
                type="AGENTS_ACTIVATED",
                summary=f"Activated {len(activated_agents_list)} agents",
                branch_id=self.branch_id,
                pointers=[],
                meta={"route_level": route_level, "agents": activated_agents_list, "style": resolved_style},
            ),
            activated_agents=activated_agents,
        )

        if route_level not in {"L0", "L1"}:
            raise NotImplementedError(f"Route level {route_level} is not implemented yet (Phase 2+).")

        router = self._agent("router")
        pm = self._agent("pm") if "pm" in activated_agents else None
        coder = self._agent("coder_backend")

        ctx = self._build_context_packet()
        self._append_guarded(
            event=new_event(
                agent="router",
                type="CONTEXT_PACKET_BUILT",
                summary="Built ContextPacket",
                branch_id=self.branch_id,
                pointers=ctx.repo_pointers,
                meta={"route_level": route_level, "style": resolved_style},
            ),
            activated_agents=activated_agents,
        )

        req: packs.RequirementPack | None = None
        if route_level != "L0":
            if not pm:
                raise RuntimeError("pm must be activated for L1+ routes")
            pm_msgs = self._messages_with_memory(
                agent_id="pm",
                system=(
                    "You are PM. Return JSON only for RequirementPack with fields: "
                    "summary (string), acceptance (string[]), non_goals (string[]), constraints (string[]). "
                    "No extra keys. No wrapping object. No markdown.\n\n"
                    f"{workflow_hint}"
                ),
                user=f"Task:\n{task_text}\n\nContextPacket:\n{ctx.model_dump_json()}",
            )
            req, _req_meta = pm.chat_json(
                schema=packs.RequirementPack,
                messages=pm_msgs,
                user=task_text,
            )
            self._append_guarded(
                event=new_event(
                    agent="pm",
                    type="AC_DEFINED",
                    summary="Acceptance criteria defined",
                    branch_id=self.branch_id,
                    pointers=[],
                    meta={"acceptance": req.acceptance, "route_level": route_level, "style": resolved_style},
                ),
                activated_agents=activated_agents,
            )

        plan_user = (
            f"RequirementPack:\n{req.model_dump_json()}\n\nContextPacket:\n{ctx.model_dump_json()}"
            if req is not None
            else f"Task:\n{task_text}\n\nContextPacket:\n{ctx.model_dump_json()}"
        )
        router_msgs = self._messages_with_memory(
            agent_id="router",
            system=(
                "You are Router. Return JSON only for Plan: {tasks:[{id,title,agent,description}]}. "
                "Keep tasks <= 5. No extra keys. No markdown.\n\n"
                f"{workflow_hint}"
            ),
            user=plan_user,
        )
        plan, _plan_meta = router.chat_json(
            schema=packs.Plan,
            messages=router_msgs,
            user=plan_user,
        )
        self._append_guarded(
            event=new_event(
                agent="router",
                type="PLAN_CREATED",
                summary=f"Planned {len(plan.tasks)} tasks",
                branch_id=self.branch_id,
                pointers=[],
                meta={"route_level": route_level, "style": resolved_style},
            ),
            activated_agents=activated_agents,
        )

        coder_user = (
            f"RequirementPack:\n{req.model_dump_json()}\n\nPlan:\n{plan.model_dump_json()}\n\nContextPacket:\n{ctx.model_dump_json()}"
            if req is not None
            else f"Task:\n{task_text}\n\nPlan:\n{plan.model_dump_json()}\n\nContextPacket:\n{ctx.model_dump_json()}"
        )
        coder_msgs = self._messages_with_memory(
            agent_id="coder_backend",
            system=(
                "You are Coder. Return JSON only for CodeChange with fields: "
                "kind ('commit'|'patch'|'noop'), summary, writes? (list[{path,content}]), commit_hash?, patch_pointer?, files_changed[], blockers[]. "
                "Prefer 'writes' for file changes (especially when starting from an empty repo). "
                "Each writes item must include the full file content. No extra keys. No markdown."
                "\n\n"
                f"{workflow_hint}"
            ),
            user=coder_user,
        )
        change, _change_meta = coder.chat_json(schema=packs.CodeChange, messages=coder_msgs, user=coder_user)
        change, write_pointers = self._materialize_code_change(change)

        self._append_guarded(
            event=new_event(
                agent="coder_backend",
                type="PATCH_WRITTEN" if change.kind == "patch" else "CODE_COMMIT",
                summary=change.summary,
                branch_id=self.branch_id,
                pointers=[p for p in [change.patch_pointer, change.commit_hash] if p] + write_pointers,
                meta={"files_changed": change.files_changed, "route_level": route_level, "style": resolved_style},
            ),
            activated_agents=activated_agents,
        )

        if self.policy.mode == "chat_only":
            checkpoint_id = f"ckpt_{uuid4().hex[:12]}"
            artifacts: List[str] = []
            if change.patch_pointer:
                artifacts.append(change.patch_pointer)
            cp = self.checkpoints.create(
                checkpoint_id=checkpoint_id,
                label=(req.summary if req is not None else task_text.strip().splitlines()[0][:120]),
                repo_ref="no-git",
                ledger_offset=self.ledger.count_lines(),
                artifacts=artifacts,
                green=False,
                restore_steps=["policy(chat_only): no restore steps recorded"],
                meta={"reason": "chat_only", "route_level": route_level, "agents": activated_agents_list, "style": resolved_style},
            )
            self._append_guarded(
                event=new_event(
                    agent="router",
                    type="CHECKPOINT_CREATED",
                    summary=f"Created checkpoint {cp.id} (non-green, chat_only)",
                    branch_id=self.branch_id,
                    pointers=artifacts,
                    meta={"green": False, "repo_ref": "no-git", "route_level": route_level, "agents": activated_agents_list, "style": resolved_style},
                ),
                activated_agents=activated_agents,
            )
            return RunResult(checkpoint_id=cp.id, green=False)

        # QA
        qa_profile = "smoke" if route_level == "L0" else "unit"
        self._append_guarded(
            event=new_event(
                agent="qa",
                type="TEST_RUN",
                summary="mock: tests skipped" if os.getenv("VIBE_MOCK_MODE", "").strip() == "1" else f"Running tests ({qa_profile})",
                branch_id=self.branch_id,
                pointers=[],
                meta={"profile": qa_profile, "route_level": route_level, "style": resolved_style},
            ),
            activated_agents=activated_agents,
        )
        report = self._run_tests(profile=qa_profile)
        self._append_guarded(
            event=new_event(
                agent="qa",
                type="TEST_PASSED" if report.passed else "TEST_FAILED",
                summary="Tests passed" if report.passed else "Tests failed",
                branch_id=self.branch_id,
                pointers=report.pointers,
                meta={"blockers": report.blockers, "profile": qa_profile, "route_level": route_level, "style": resolved_style},
            ),
            activated_agents=activated_agents,
        )

        if route_level == "L0":
            # L0 never produces green checkpoints.
            artifacts: List[str] = []
            if change.patch_pointer:
                artifacts.append(change.patch_pointer)
            artifacts.extend(report.pointers)
            checkpoint_id = f"ckpt_{uuid4().hex[:12]}"
            cp = self.checkpoints.create(
                checkpoint_id=checkpoint_id,
                label=task_text.strip().splitlines()[0][:120],
                repo_ref="no-git",
                ledger_offset=self.ledger.count_lines(),
                artifacts=artifacts,
                green=False,
                restore_steps=["L0(draft): re-run with L1+ to get green"],
                meta={"draft": True, "route_level": route_level, "agents": activated_agents_list, "qa_profile": qa_profile},
            )
            self._append_guarded(
                event=new_event(
                    agent="router",
                    type="CHECKPOINT_CREATED",
                    summary=f"Created draft checkpoint {cp.id} (L0)",
                    branch_id=self.branch_id,
                    pointers=artifacts,
                    meta={"green": False, "repo_ref": "no-git", "route_level": route_level, "agents": activated_agents_list},
                ),
                activated_agents=activated_agents,
            )
            return RunResult(checkpoint_id=cp.id, green=False)

        # L1+ gates: if we couldn't find any QA commands, do not mark green.
        if not report.commands:
            artifacts: List[str] = []
            if change.patch_pointer:
                artifacts.append(change.patch_pointer)
            artifacts.extend(report.pointers)
            checkpoint_id = f"ckpt_{uuid4().hex[:12]}"
            cp = self.checkpoints.create(
                checkpoint_id=checkpoint_id,
                label=(req.summary if req is not None else task_text.strip().splitlines()[0][:120]),
                repo_ref="no-git",
                ledger_offset=self.ledger.count_lines(),
                artifacts=artifacts,
                green=False,
                restore_steps=["QA: no test commands detected; configure tests/lint then re-run"],
                meta={
                    "route_level": route_level,
                    "agents": activated_agents_list,
                    "qa_profile": qa_profile,
                    "reason": "qa_no_commands",
                    "style": resolved_style,
                },
            )
            self._append_guarded(
                event=new_event(
                    agent="router",
                    type="CHECKPOINT_CREATED",
                    summary=f"Created checkpoint {cp.id} (non-green, no QA commands)",
                    branch_id=self.branch_id,
                    pointers=artifacts,
                    meta={"green": False, "repo_ref": "no-git", "route_level": route_level, "agents": activated_agents_list, "style": resolved_style},
                ),
                activated_agents=activated_agents,
            )
            return RunResult(checkpoint_id=cp.id, green=False)

        if not report.passed:
            max_loops = 3
            loop = 0
            while loop < max_loops and not report.passed:
                loop += 1
                blocker = (report.blockers or ["tests failed"])[0]
                fix_user = f"Blocker:\n{blocker}\n\nRequirementPack:\n{req.model_dump_json()}\n\nContextPacket:\n{ctx.model_dump_json()}"
                fix_msgs = self._messages_with_memory(
                    agent_id="coder_backend",
                    system=(
                        "You are Coder. Fix exactly one blocker. Return JSON only for CodeChange with fields: "
                        "kind ('commit'|'patch'|'noop'), summary, writes? (list[{path,content}]), commit_hash?, patch_pointer?, files_changed[], blockers[]. "
                        "Prefer 'writes' for file changes. No extra keys. No markdown.\n\n"
                        f"{workflow_hint}"
                    ),
                    user=fix_user,
                )
                change, _ = coder.chat_json(schema=packs.CodeChange, messages=fix_msgs, user=fix_user)
                change, write_pointers = self._materialize_code_change(change)
                self._append_guarded(
                    event=new_event(
                        agent="coder_backend",
                        type="PATCH_WRITTEN" if change.kind == "patch" else "CODE_COMMIT",
                        summary=f"fix-loop {loop}: {change.summary}",
                        branch_id=self.branch_id,
                        pointers=[p for p in [change.patch_pointer, change.commit_hash] if p] + write_pointers,
                        meta={"blocker": blocker, "route_level": route_level, "style": resolved_style},
                    ),
                    activated_agents=activated_agents,
                )
                self._append_guarded(
                    event=new_event(
                        agent="qa",
                        type="TEST_RUN",
                        summary=f"Fix-loop {loop}: re-running tests",
                        branch_id=self.branch_id,
                        pointers=[],
                        meta={"profile": qa_profile, "route_level": route_level, "style": resolved_style},
                    ),
                    activated_agents=activated_agents,
                )
                report = self._run_tests(profile=qa_profile)
                self._append_guarded(
                    event=new_event(
                        agent="qa",
                        type="TEST_PASSED" if report.passed else "TEST_FAILED",
                        summary="Tests passed" if report.passed else "Tests failed",
                        branch_id=self.branch_id,
                        pointers=report.pointers,
                        meta={"blockers": report.blockers, "loop": loop, "profile": qa_profile, "route_level": route_level, "style": resolved_style},
                    ),
                    activated_agents=activated_agents,
                )
            if not report.passed:
                raise RuntimeError("Tests failed after fix-loop.")

        # Create green checkpoint
        artifacts: List[str] = []
        if change.patch_pointer:
            artifacts.append(change.patch_pointer)
        artifacts.extend(report.pointers)

        repo_ref = "no-git"
        try:
            repo_ref = self.toolbox.git_head_sha(agent_id="router")
        except Exception:
            snap = self.checkpoints.snapshot_repo()
            artifacts.append(snap.to_pointer())

        checkpoint_id = f"ckpt_{uuid4().hex[:12]}"
        restore_steps = (
            [f"git checkout --detach {repo_ref}"] if repo_ref != "no-git" else [f"vibe checkpoint restore {checkpoint_id}"]
        )
        cp = self.checkpoints.create(
            checkpoint_id=checkpoint_id,
            label=(req.summary if req is not None else task_text.strip().splitlines()[0][:120]),
            repo_ref=repo_ref,
            ledger_offset=self.ledger.count_lines(),
            artifacts=artifacts,
            green=True,
            restore_steps=restore_steps,
            meta={"route_level": route_level, "agents": activated_agents_list, "qa_profile": qa_profile, "style": resolved_style},
        )
        self._append_guarded(
            event=new_event(
                agent="router",
                type="CHECKPOINT_CREATED",
                summary=f"Created green checkpoint {cp.id}",
                branch_id=self.branch_id,
                pointers=artifacts,
                meta={"green": True, "repo_ref": repo_ref, "route_level": route_level, "agents": activated_agents_list, "style": resolved_style},
            ),
            activated_agents=activated_agents,
        )

        return RunResult(checkpoint_id=cp.id, green=True)
