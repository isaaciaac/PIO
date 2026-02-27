from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
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

    def _run_tests(self) -> packs.TestReport:
        if os.getenv("VIBE_MOCK_MODE", "").strip() == "1":
            return packs.TestReport(
                commands=["mock"],
                results=[packs.TestResult(command="mock", returncode=0, passed=True, stdout="", stderr="")],
                passed=True,
                blockers=[],
                pointers=[],
            )

        commands: List[str] = []
        if (self.repo_root / "pyproject.toml").exists() or (self.repo_root / "tests").exists():
            commands = ["pytest -q"]
        elif (self.repo_root / "package.json").exists():
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

    def run(self, *, task_id: Optional[str] = None) -> RunResult:
        task_evt = self._find_task(task_id)
        task_text = str(task_evt.meta.get("text") or task_evt.summary)

        router = self._agent("router")
        pm = self._agent("pm")
        coder = self._agent("coder_backend")

        ctx = self._build_context_packet()
        self.ledger.append(
            new_event(
                agent="router",
                type="CONTEXT_PACKET_BUILT",
                summary="Built ContextPacket",
                branch_id=self.branch_id,
                pointers=ctx.repo_pointers,
            )
        )

        req, _req_meta = pm.chat_json(
            schema=packs.RequirementPack,
            system="You are PM. Output JSON for RequirementPack only.",
            user=f"Task:\n{task_text}\n\nContextPacket:\n{ctx.model_dump_json()}",
        )
        self.ledger.append(
            new_event(
                agent="pm",
                type="AC_DEFINED",
                summary="Acceptance criteria defined",
                branch_id=self.branch_id,
                pointers=[],
                meta={"acceptance": req.acceptance},
            )
        )

        plan, _plan_meta = router.chat_json(
            schema=packs.Plan,
            system="You are Router. Output JSON for Plan only.",
            user=f"RequirementPack:\n{req.model_dump_json()}\n\nContextPacket:\n{ctx.model_dump_json()}",
        )
        self.ledger.append(
            new_event(
                agent="router",
                type="PLAN_CREATED",
                summary=f"Planned {len(plan.tasks)} tasks",
                branch_id=self.branch_id,
                pointers=[],
            )
        )

        change, _change_meta = coder.chat_json(
            schema=packs.CodeChange,
            system="You are Coder. Output JSON for CodeChange only.",
            user=f"RequirementPack:\n{req.model_dump_json()}\n\nPlan:\n{plan.model_dump_json()}\n\nContextPacket:\n{ctx.model_dump_json()}",
        )
        if change.kind == "noop" or not change.patch_pointer:
            patch_ptr = self.artifacts.put_text("mock: no code changes", suffix=".patch.txt", kind="patch").to_pointer()
            change = packs.CodeChange(kind="patch", summary=change.summary or "mock patch", patch_pointer=patch_ptr, files_changed=change.files_changed)

        self.ledger.append(
            new_event(
                agent="coder_backend",
                type="PATCH_WRITTEN" if change.kind == "patch" else "CODE_COMMIT",
                summary=change.summary,
                branch_id=self.branch_id,
                pointers=[p for p in [change.patch_pointer, change.commit_hash] if p],
                meta={"files_changed": change.files_changed},
            )
        )

        if self.policy.mode == "chat_only":
            checkpoint_id = f"ckpt_{uuid4().hex[:12]}"
            artifacts: List[str] = []
            if change.patch_pointer:
                artifacts.append(change.patch_pointer)
            cp = self.checkpoints.create(
                checkpoint_id=checkpoint_id,
                label=req.summary,
                repo_ref="no-git",
                ledger_offset=self.ledger.count_lines(),
                artifacts=artifacts,
                green=False,
                restore_steps=["policy(chat_only): no restore steps recorded"],
                meta={"reason": "chat_only"},
            )
            self.ledger.append(
                new_event(
                    agent="router",
                    type="CHECKPOINT_CREATED",
                    summary=f"Created checkpoint {cp.id} (non-green, chat_only)",
                    branch_id=self.branch_id,
                    pointers=artifacts,
                    meta={"green": False, "repo_ref": "no-git"},
                )
            )
            return RunResult(checkpoint_id=cp.id, green=False)

        # QA
        self.ledger.append(
            new_event(
                agent="qa",
                type="TEST_RUN",
                summary="mock: tests skipped" if os.getenv("VIBE_MOCK_MODE", "").strip() == "1" else "Running tests",
                branch_id=self.branch_id,
                pointers=[],
            )
        )
        report = self._run_tests()
        self.ledger.append(
            new_event(
                agent="qa",
                type="TEST_PASSED" if report.passed else "TEST_FAILED",
                summary="Tests passed" if report.passed else "Tests failed",
                branch_id=self.branch_id,
                pointers=report.pointers,
                meta={"blockers": report.blockers},
            )
        )

        if not report.passed:
            max_loops = 3
            loop = 0
            while loop < max_loops and not report.passed:
                loop += 1
                blocker = (report.blockers or ["tests failed"])[0]
                change, _ = coder.chat_json(
                    schema=packs.CodeChange,
                    system="You are Coder. Fix exactly one blocker. Output JSON for CodeChange only.",
                    user=f"Blocker:\n{blocker}\n\nRequirementPack:\n{req.model_dump_json()}\n\nContextPacket:\n{ctx.model_dump_json()}",
                )
                patch_ptr = self.artifacts.put_text(
                    f"fix-loop {loop}: {blocker}\n",
                    suffix=".patch.txt",
                    kind="patch",
                ).to_pointer()
                if change.kind != "commit":
                    change = packs.CodeChange(kind="patch", summary=change.summary or f"fix {blocker}", patch_pointer=patch_ptr)
                self.ledger.append(
                    new_event(
                        agent="coder_backend",
                        type="PATCH_WRITTEN" if change.kind == "patch" else "CODE_COMMIT",
                        summary=f"fix-loop {loop}: {change.summary}",
                        branch_id=self.branch_id,
                        pointers=[p for p in [change.patch_pointer, change.commit_hash] if p],
                        meta={"blocker": blocker},
                    )
                )
                self.ledger.append(
                    new_event(
                        agent="qa",
                        type="TEST_RUN",
                        summary=f"Fix-loop {loop}: re-running tests",
                        branch_id=self.branch_id,
                        pointers=[],
                    )
                )
                report = self._run_tests()
                self.ledger.append(
                    new_event(
                        agent="qa",
                        type="TEST_PASSED" if report.passed else "TEST_FAILED",
                        summary="Tests passed" if report.passed else "Tests failed",
                        branch_id=self.branch_id,
                        pointers=report.pointers,
                        meta={"blockers": report.blockers, "loop": loop},
                    )
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
            label=req.summary,
            repo_ref=repo_ref,
            ledger_offset=self.ledger.count_lines(),
            artifacts=artifacts,
            green=True,
            restore_steps=restore_steps,
        )
        self.ledger.append(
            new_event(
                agent="router",
                type="CHECKPOINT_CREATED",
                summary=f"Created green checkpoint {cp.id}",
                branch_id=self.branch_id,
                pointers=artifacts,
                meta={"green": True, "repo_ref": repo_ref},
            )
        )

        return RunResult(checkpoint_id=cp.id, green=True)
