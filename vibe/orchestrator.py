from __future__ import annotations

import json
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
from vibe.routes import DiffStats, decide_route, detect_risks
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

    def _build_context_packet(self) -> tuple[packs.ContextPacket, str]:
        pointers: List[str] = []
        excerpts: List[str] = []
        for rel in [".vibe/manifests/project_manifest.md", ".vibe/manifests/run_manifest.md", "README.md"]:
            path = self.repo_root / rel
            if not path.exists():
                continue
            try:
                rr = self.toolbox.read_file(agent_id="router", path=rel, start_line=1, end_line=200)
                pointers.append(rr.pointer)
                excerpts.append(f"<<< {rr.pointer} >>>\n{rr.content}\n")
            except Exception:
                # best-effort; context snippets are helpful but should not block the workflow
                continue

        recent = []
        for evt in self.ledger.iter_events(limit=20, reverse=True):
            recent.append(packs.ContextEventRef(id=evt.id, summary=evt.summary, pointers=evt.pointers))
        ctx = packs.ContextPacket(repo_pointers=pointers, recent_events=recent)
        return ctx, ("\n".join(excerpts).strip() if excerpts else "")

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

    def _find_node_project_dir(self) -> Optional[Path]:
        """Return relative directory that contains a package.json, if any."""
        root_pkg = self.repo_root / "package.json"
        if root_pkg.exists():
            return Path(".")

        for rel in ["client", "frontend", "web", "app", "backend", "server"]:
            if (self.repo_root / rel / "package.json").exists():
                return Path(rel)

        # Best-effort shallow search (avoid scanning large repos).
        try:
            for p in self.repo_root.rglob("package.json"):
                rel = p.relative_to(self.repo_root)
                if rel.parts and rel.parts[0] in {".git", ".vibe", "node_modules", "dist", "build"}:
                    continue
                if len(rel.parts) <= 2:
                    return rel.parent
        except Exception:
            return None
        return None

    def _package_manager(self, node_dir: Path) -> str:
        root = self.repo_root
        if (root / node_dir / "pnpm-lock.yaml").exists() or (root / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (root / node_dir / "yarn.lock").exists() or (root / "yarn.lock").exists():
            return "yarn"
        return "npm"

    def _shell_cmd_in_dir(self, *, rel_dir: Path, cmd: str) -> str:
        d = rel_dir.as_posix().strip()
        if not d or d == ".":
            return cmd
        if os.name == "nt":
            return f'cd /d "{d}" && {cmd}'
        return f'cd "{d}" && {cmd}'

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

        # If the model returned a unified diff patch but no inline writes, try to apply it
        # locally so the repo actually changes. This keeps the workflow auditable via
        # cmd artifacts, and avoids "said it generated code but nothing changed".
        if not write_pointers and not change.writes and change.patch_pointer and "@sha256:" in change.patch_pointer:
            patch_text = self.artifacts.read_bytes(change.patch_pointer).decode("utf-8", errors="replace")
            looks_like_patch = ("diff --git " in patch_text) or (patch_text.lstrip().startswith("--- ") and "\n+++ " in patch_text)
            if looks_like_patch:
                paths: set[str] = set()
                for line in patch_text.splitlines():
                    if line.startswith("diff --git "):
                        parts = line.split()
                        if len(parts) >= 4:
                            for p in (parts[2], parts[3]):
                                p = p[2:] if p.startswith(("a/", "b/")) else p
                                paths.add(p)
                    elif line.startswith(("--- ", "+++ ")):
                        parts = line.split(maxsplit=1)
                        if len(parts) == 2:
                            p = parts[1].strip()
                            if p == "/dev/null":
                                continue
                            p = p[2:] if p.startswith(("a/", "b/")) else p
                            paths.add(p)

                for p in sorted(paths):
                    rel = p.replace("\\", "/").lstrip("/")
                    if rel.startswith(".vibe/") or rel.startswith(".git/"):
                        raise RuntimeError(f"Refusing to apply patch touching internal path: {p}")
                    if ":" in rel or rel.startswith("\\\\") or rel.startswith("//") or "/../" in f"/{rel}/":
                        raise RuntimeError(f"Refusing to apply patch with unsafe path: {p}")

                patch_path = change.patch_pointer.split("@sha256:", 1)[0]
                abs_patch = (self.repo_root / patch_path).resolve()
                r = self.toolbox.run_cmd(
                    agent_id="coder_backend",
                    cmd=["git", "apply", "--whitespace=nowarn", str(abs_patch)],
                    cwd=self.repo_root,
                    timeout_s=600,
                )
                write_pointers.extend([r.stdout, r.stderr, r.meta])
                if r.returncode != 0:
                    err = self.artifacts.read_bytes(r.stderr).decode("utf-8", errors="replace").strip()
                    raise RuntimeError(f"Failed to apply patch via git apply (code={r.returncode}). {err}")

                if not change.files_changed and paths:
                    change.files_changed = sorted({p.replace("\\", "/").lstrip("/") for p in paths})

        if change.kind == "noop" or (not change.patch_pointer and not change.commit_hash and not write_pointers):
            patch_ptr = self.artifacts.put_text("mock: no code changes", suffix=".patch.txt", kind="patch").to_pointer()
            change = packs.CodeChange(kind="patch", summary=change.summary or "mock patch", patch_pointer=patch_ptr, files_changed=change.files_changed)

        return change, write_pointers

    def _determine_test_commands(self, *, profile: str) -> List[str]:
        if os.getenv("VIBE_MOCK_MODE", "").strip() == "1":
            return ["mock"]

        is_py = (self.repo_root / "pyproject.toml").exists() or (self.repo_root / "tests").exists()
        node_dir = self._find_node_project_dir()
        is_node = node_dir is not None
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
                return ["python -m compileall ."]
            if is_node and node_dir is not None:
                pm = self._package_manager(node_dir)
                scripts: dict[str, str] = {}
                try:
                    pkg = json.loads((self.repo_root / node_dir / "package.json").read_text(encoding="utf-8", errors="replace"))
                    scripts = dict(pkg.get("scripts") or {})
                except Exception:
                    scripts = {}

                if "build" in scripts:
                    return [self._shell_cmd_in_dir(rel_dir=node_dir, cmd=f"{pm} run build")]
                if "lint" in scripts:
                    return [self._shell_cmd_in_dir(rel_dir=node_dir, cmd=f"{pm} run lint")]
                if "test" in scripts:
                    return [self._shell_cmd_in_dir(rel_dir=node_dir, cmd=f"{pm} test")]
                return []
            return []

        if is_py:
            # Prefer pytest when available and the project appears to have tests.
            if has_py_tests and self._python_has_module("pytest"):
                return ["python -m compileall .", "pytest -q"]
            return ["python -m compileall .", "python -m unittest -q"]
        if is_node and node_dir is not None:
            pm = self._package_manager(node_dir)
            scripts: dict[str, str] = {}
            try:
                pkg = json.loads((self.repo_root / node_dir / "package.json").read_text(encoding="utf-8", errors="replace"))
                scripts = dict(pkg.get("scripts") or {})
            except Exception:
                scripts = {}

            cmds: list[str] = []
            if "test" in scripts:
                cmds.append(self._shell_cmd_in_dir(rel_dir=node_dir, cmd=f"{pm} test"))
            if "lint" in scripts:
                cmds.append(self._shell_cmd_in_dir(rel_dir=node_dir, cmd=f"{pm} run lint"))
            if not cmds and "build" in scripts:
                cmds.append(self._shell_cmd_in_dir(rel_dir=node_dir, cmd=f"{pm} run build"))
            return cmds
        return []

    def _run_tests(self, *, profile: str, commands: Optional[List[str]] = None) -> packs.TestReport:
        cmds = list(commands) if commands is not None else self._determine_test_commands(profile=profile)

        if cmds == ["mock"]:
            return packs.TestReport(
                commands=["mock"],
                results=[packs.TestResult(command="mock", returncode=0, passed=True, stdout="", stderr="")],
                passed=True,
                blockers=[],
                pointers=[],
            )

        if not cmds:
            return packs.TestReport(commands=[], results=[], passed=True, blockers=[], pointers=[])

        results: List[packs.TestResult] = []
        blockers: List[str] = []
        pointers: List[str] = []
        for cmd in cmds:
            r = self.toolbox.run_cmd(agent_id="qa", cmd=cmd, cwd=self.repo_root, timeout_s=1800)
            passed = r.returncode == 0
            results.append(
                packs.TestResult(command=cmd, returncode=r.returncode, passed=passed, stdout=r.stdout, stderr=r.stderr, meta=r.meta)
            )
            pointers.extend([r.stdout, r.stderr, r.meta])
            if not passed:
                blockers.append(f"Command failed: {cmd}")

        return packs.TestReport(commands=cmds, results=results, passed=all(x.passed for x in results), blockers=blockers, pointers=pointers)

    def run(self, *, task_id: Optional[str] = None, route: Optional[str] = None, style: Optional[str] = None) -> RunResult:
        task_evt = self._find_task(task_id)
        task_text = str(task_evt.meta.get("text") or task_evt.summary)

        resolved_style = normalize_style(style or os.getenv("VIBE_STYLE") or getattr(self.config.behavior, "style", "balanced"))
        workflow_hint = style_workflow_hint(resolved_style)

        diff = self._git_diff_stats_best_effort()
        risks = detect_risks(task_text, diff=diff)
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

        if route_level not in {"L0", "L1", "L2"}:
            raise NotImplementedError(f"Route level {route_level} is not implemented yet (Phase 3+).")

        router = self._agent("router")
        pm = self._agent("pm") if "pm" in activated_agents else None
        req_analyst = self._agent("requirements_analyst") if "requirements_analyst" in activated_agents else None
        architect = self._agent("architect") if "architect" in activated_agents else None
        api_confirm = self._agent("api_confirm") if "api_confirm" in activated_agents else None
        coder = self._agent("coder_backend")
        reviewer = self._agent("code_reviewer") if "code_reviewer" in activated_agents else None

        ctx, ctx_excerpts = self._build_context_packet()
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
        usecases: Optional[packs.UseCasePack] = None
        usecases_ptr: Optional[str] = None
        decisions: Optional[packs.DecisionPack] = None
        decisions_ptr: Optional[str] = None
        contract: Optional[packs.ContractPack] = None
        contract_ptr: Optional[str] = None
        if route_level != "L0":
            if not pm:
                raise RuntimeError("pm must be activated for L1+ routes")
            pm_user = f"Task:\n{task_text}\n\nContextPacket:\n{ctx.model_dump_json()}"
            if ctx_excerpts:
                pm_user = f"{pm_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
            pm_msgs = self._messages_with_memory(
                agent_id="pm",
                system=(
                    "You are PM. Return JSON only for RequirementPack with fields: "
                    "summary (string), acceptance (string[]), non_goals (string[]), constraints (string[]). "
                    "No extra keys. No wrapping object. No markdown.\n\n"
                    f"{workflow_hint}"
                ),
                user=pm_user,
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

        if route_level == "L2":
            if req_analyst:
                ua_user = f"Task:\n{task_text}\n\nRequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\nContextPacket:\n{ctx.model_dump_json()}"
                if ctx_excerpts:
                    ua_user = f"{ua_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
                ua_msgs = self._messages_with_memory(
                    agent_id="requirements_analyst",
                    system=(
                        "You are Requirements Analyst. Return JSON only for UseCasePack with fields: "
                        "positive (string[]), negative (string[]), edge_cases (string[]). "
                        "No extra keys. No wrapping object. No markdown.\n\n"
                        f"{workflow_hint}"
                    ),
                    user=ua_user,
                )
                usecases, _ = req_analyst.chat_json(schema=packs.UseCasePack, messages=ua_msgs, user=ua_user)
                usecases_ptr = self.artifacts.put_json(usecases.model_dump(), suffix=".usecases.json", kind="usecases").to_pointer()
                self._append_guarded(
                    event=new_event(
                        agent="requirements_analyst",
                        type="USECASES_DEFINED",
                        summary="Use cases defined",
                        branch_id=self.branch_id,
                        pointers=[usecases_ptr],
                        meta={"route_level": route_level, "style": resolved_style},
                    ),
                    activated_agents=activated_agents,
                )

            if architect:
                adr_user = (
                    f"Task:\n{task_text}\n\nRequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                    f"UseCasePack:\n{usecases.model_dump_json() if usecases is not None else '{}'}\n\nContextPacket:\n{ctx.model_dump_json()}"
                )
                if ctx_excerpts:
                    adr_user = f"{adr_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
                adr_msgs = self._messages_with_memory(
                    agent_id="architect",
                    system=(
                        "You are Architect. Produce an ADR-lite. Return JSON only for DecisionPack with fields: "
                        "adrs (list[object]). Each adr should include at least: title, context, decision, consequences. "
                        "No extra keys. No wrapping object. No markdown.\n\n"
                        f"{workflow_hint}"
                    ),
                    user=adr_user,
                )
                decisions, _ = architect.chat_json(schema=packs.DecisionPack, messages=adr_msgs, user=adr_user)
                decisions_ptr = self.artifacts.put_json(decisions.model_dump(), suffix=".adr.json", kind="adr").to_pointer()
                self._append_guarded(
                    event=new_event(
                        agent="architect",
                        type="ADR_ADDED",
                        summary="ADR-lite added",
                        branch_id=self.branch_id,
                        pointers=[decisions_ptr],
                        meta={"route_level": route_level, "style": resolved_style},
                    ),
                    activated_agents=activated_agents,
                )

            if api_confirm and (risks.contract_change or risks.touches_external_api):
                contract_user = (
                    f"Task:\n{task_text}\n\nRequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                    f"UseCasePack:\n{usecases.model_dump_json() if usecases is not None else '{}'}\n\nContextPacket:\n{ctx.model_dump_json()}"
                )
                if ctx_excerpts:
                    contract_user = f"{contract_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
                contract_msgs = self._messages_with_memory(
                    agent_id="api_confirm",
                    system=(
                        "You are API/Contract confirmer. Return JSON only for ContractPack with fields: "
                        "contracts (list[object]). Use minimal, stable contracts: endpoints/schemas/examples when applicable. "
                        "No extra keys. No wrapping object. No markdown.\n\n"
                        f"{workflow_hint}"
                    ),
                    user=contract_user,
                )
                contract, _ = api_confirm.chat_json(schema=packs.ContractPack, messages=contract_msgs, user=contract_user)
                contract_ptr = self.artifacts.put_json(contract.model_dump(), suffix=".contract.json", kind="contract").to_pointer()
                self._append_guarded(
                    event=new_event(
                        agent="api_confirm",
                        type="CONTRACT_CONFIRMED",
                        summary="Contract confirmed",
                        branch_id=self.branch_id,
                        pointers=[contract_ptr],
                        meta={"route_level": route_level, "style": resolved_style},
                    ),
                    activated_agents=activated_agents,
                )

        plan_user = (
            f"RequirementPack:\n{req.model_dump_json()}\n\nContextPacket:\n{ctx.model_dump_json()}"
            if req is not None
            else f"Task:\n{task_text}\n\nContextPacket:\n{ctx.model_dump_json()}"
        )
        if ctx_excerpts:
            plan_user = f"{plan_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
        if usecases is not None:
            plan_user = f"{plan_user}\n\nUseCasePack:\n{usecases.model_dump_json()}"
        if decisions is not None:
            plan_user = f"{plan_user}\n\nDecisionPack:\n{decisions.model_dump_json()}"
        if contract is not None:
            plan_user = f"{plan_user}\n\nContractPack:\n{contract.model_dump_json()}"
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
        if ctx_excerpts:
            coder_user = f"{coder_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
        if usecases is not None:
            coder_user = f"{coder_user}\n\nUseCasePack:\n{usecases.model_dump_json()}"
        if decisions is not None:
            coder_user = f"{coder_user}\n\nDecisionPack:\n{decisions.model_dump_json()}"
        if contract is not None:
            coder_user = f"{coder_user}\n\nContractPack:\n{contract.model_dump_json()}"
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
        qa_profile = "smoke" if route_level == "L0" else ("unit" if route_level == "L1" else "full")
        qa_commands = self._determine_test_commands(profile=qa_profile)
        mock_mode = os.getenv("VIBE_MOCK_MODE", "").strip() == "1"
        if mock_mode:
            test_run_summary = "mock: tests skipped"
        elif not qa_commands:
            test_run_summary = "No QA commands detected; skipping tests"
        else:
            test_run_summary = f"Running tests ({qa_profile})"
        self._append_guarded(
            event=new_event(
                agent="qa",
                type="TEST_RUN",
                summary=test_run_summary,
                branch_id=self.branch_id,
                pointers=[],
                meta={"profile": qa_profile, "commands": qa_commands, "route_level": route_level, "style": resolved_style},
            ),
            activated_agents=activated_agents,
        )
        report = self._run_tests(profile=qa_profile, commands=qa_commands)
        self._append_guarded(
            event=new_event(
                agent="qa",
                type="TEST_PASSED" if report.passed else "TEST_FAILED",
                summary=(
                    "No QA commands detected; tests skipped"
                    if (not mock_mode and not report.commands)
                    else ("Tests passed" if report.passed else "Tests failed")
                ),
                branch_id=self.branch_id,
                pointers=report.pointers,
                meta={
                    "blockers": report.blockers,
                    "profile": qa_profile,
                    "commands": report.commands,
                    "route_level": route_level,
                    "style": resolved_style,
                },
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

        review: Optional[packs.ReviewReport] = None
        review_ptr: Optional[str] = None

        def run_review() -> tuple[packs.ReviewReport, str]:
            if not reviewer:
                raise RuntimeError("code_reviewer must be activated for L2 routes")

            review_user = (
                f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                f"UseCasePack:\n{usecases.model_dump_json() if usecases is not None else '{}'}\n\n"
                f"DecisionPack:\n{decisions.model_dump_json() if decisions is not None else '{}'}\n\n"
                f"ContractPack:\n{contract.model_dump_json() if contract is not None else '{}'}\n\n"
                f"CodeChange:\n{change.model_dump_json()}\n\n"
                f"TestReport:\n{report.model_dump_json()}\n\n"
                f"ContextPacket:\n{ctx.model_dump_json()}"
            )
            if ctx_excerpts:
                review_user = f"{review_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
            review_msgs = self._messages_with_memory(
                agent_id="code_reviewer",
                system=(
                    "You are Code Reviewer. You must be strict on blockers (security, correctness, data loss, breaking changes). "
                    "Return JSON only for ReviewReport with fields: passed (bool), blockers (string[]), nits (string[]), pointers (string[]). "
                    "No extra keys. No wrapping object. No markdown.\n\n"
                    f"{workflow_hint}"
                ),
                user=review_user,
            )
            rr, _ = reviewer.chat_json(schema=packs.ReviewReport, messages=review_msgs, user=review_user)
            ptr = self.artifacts.put_json(rr.model_dump(), suffix=".review.json", kind="review").to_pointer()
            passed = bool(rr.passed) and not (rr.blockers or [])
            self._append_guarded(
                event=new_event(
                    agent="code_reviewer",
                    type="REVIEW_PASSED" if passed else "REVIEW_BLOCKED",
                    summary="Review passed" if passed else "Review blocked",
                    branch_id=self.branch_id,
                    pointers=[ptr] + list(rr.pointers or []),
                    meta={"route_level": route_level, "style": resolved_style, "blockers": rr.blockers, "nits": rr.nits},
                ),
                activated_agents=activated_agents,
            )
            return rr, ptr

        review_failed = False
        if route_level == "L2" and reviewer and report.passed:
            review, review_ptr = run_review()
            review_failed = (not review.passed) or bool(review.blockers)

        if (not report.passed) or review_failed:
            max_loops = 3
            loop = 0
            while loop < max_loops and ((not report.passed) or review_failed):
                loop += 1
                blocker_source = "tests" if not report.passed else "review"
                if blocker_source == "review":
                    blocker = ((review.blockers or []) if review is not None else [])[:1] or ["review blocked"]
                    blocker_text = blocker[0]
                else:
                    blocker_text = (report.blockers or ["tests failed"])[0]

                fix_user = (
                    f"BlockerSource: {blocker_source}\n"
                    f"Blocker:\n{blocker_text}\n\n"
                    f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                    f"UseCasePack:\n{usecases.model_dump_json() if usecases is not None else '{}'}\n\n"
                    f"DecisionPack:\n{decisions.model_dump_json() if decisions is not None else '{}'}\n\n"
                    f"ContractPack:\n{contract.model_dump_json() if contract is not None else '{}'}\n\n"
                    f"ReviewReport:\n{review.model_dump_json() if review is not None else '{}'}\n\n"
                    f"TestReport:\n{report.model_dump_json()}\n\n"
                    f"ContextPacket:\n{ctx.model_dump_json()}"
                )
                if ctx_excerpts:
                    fix_user = f"{fix_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
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
                        meta={"blocker": blocker_text, "blocker_source": blocker_source, "route_level": route_level, "style": resolved_style},
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

                review_failed = False
                review = None
                review_ptr = None
                if route_level == "L2" and reviewer and report.passed:
                    review, review_ptr = run_review()
                    review_failed = (not review.passed) or bool(review.blockers)

            if (not report.passed) or review_failed:
                raise RuntimeError("Blockers remain after fix-loop.")

        # Create green checkpoint
        artifacts: List[str] = []
        artifacts.extend([p for p in [usecases_ptr, decisions_ptr, contract_ptr, review_ptr] if p])
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
