from __future__ import annotations

import json
import fnmatch
import hashlib
import os
import importlib.util
import re
import difflib
import platform
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple
from uuid import uuid4

from vibe.agents.registry import AGENT_REGISTRY
from vibe.config import VibeConfig
from vibe.policy import PolicyDeniedError, ToolPolicy, resolve_policy_mode
from vibe.schemas import packs
from vibe.schemas.events import LedgerEvent, new_event
from vibe.storage.artifacts import ArtifactsStore
from vibe.storage.checkpoints import CheckpointsStore, Checkpoint
from vibe.storage.ledger import Ledger
from vibe.storage.ledger import ledger_path
from vibe.toolbox import Toolbox
from vibe.repo import ensure_vibe_dirs
from vibe.routes import DiffStats, decide_route, detect_risks
from vibe.routes import RiskSignals
from vibe.context import append_memory_record, effective_context_config, read_memory_records
from vibe.delivery import augment_plan, augment_requirement_pack
from vibe.schemas.memory import ChatDigest, MemoryRecord
from vibe.style import normalize_style, style_workflow_hint
from vibe.text import decode_bytes
from vibe.ownership import OwnershipDeniedError
from vibe.knowledge.base import best_knowledge_snippet


@dataclass(frozen=True)
class RunResult:
    checkpoint_id: str
    green: bool


class WriteScopeDeniedError(RuntimeError):
    def __init__(self, *, path: str, allow: list[str], deny: list[str]) -> None:
        allow_preview = ", ".join(list(allow or [])[:6])
        deny_preview = ", ".join(list(deny or [])[:6])
        msg = f"Write scope denied: {path}".strip()
        if allow_preview:
            msg = f"{msg} (allow=[{allow_preview}{'…' if len(list(allow or [])) > 6 else ''}])"
        if deny_preview:
            msg = f"{msg} (deny=[{deny_preview}{'…' if len(list(deny or [])) > 6 else ''}])"
        super().__init__(msg)
        self.path = path
        self.allow = list(allow or [])
        self.deny = list(deny or [])


AUTO_RESUME_REASONS = {"fix_loop_blockers", "qa_no_commands", "replan_required"}
REPLAN_HINT_KEYWORDS = (
    "architecture",
    "architect",
    "adr",
    "api",
    "boundary",
    "contract",
    "cross-module",
    "cross module",
    "directory",
    "envspec",
    "interface",
    "module",
    "ownership",
    "plan",
    "replan",
    "route",
    "router",
    "schema",
    "shared_context",
    "shared context",
)


def _normalize_scope_pattern(pat: str) -> str:
    return (str(pat or "").replace("\\", "/").strip()).lstrip("/")


def _matches_scope_pattern(rel: str, pat: str) -> bool:
    r = _normalize_scope_pattern(rel).lower()
    p = _normalize_scope_pattern(pat).lower()
    if not p:
        return False
    if any(ch in p for ch in ["*", "?", "["]):
        try:
            return fnmatch.fnmatch(r, p)
        except Exception:
            return False
    p2 = p.rstrip("/")
    if not p2:
        return False
    return r == p2 or r.startswith(p2 + "/")


def _in_write_scope(rel: str, *, allow: list[str], deny: list[str]) -> bool:
    r = _normalize_scope_pattern(rel)
    if not r:
        return False
    d = [p for p in (deny or []) if _normalize_scope_pattern(p)]
    a = [p for p in (allow or []) if _normalize_scope_pattern(p)]
    if d and any(_matches_scope_pattern(r, p) for p in d[:200]):
        return False
    if a and not any(_matches_scope_pattern(r, p) for p in a[:400]):
        return False
    return True


class Orchestrator:
    def __init__(self, repo_root: Path, *, policy_mode: Optional[str] = None) -> None:
        self.repo_root = repo_root
        cfg_path = repo_root / ".vibe" / "vibe.yaml"
        if not cfg_path.exists():
            raise FileNotFoundError("Missing .vibe/vibe.yaml. Run `vibe init` first.")
        self.config = VibeConfig.load(cfg_path)

        # Ensure any newly introduced .vibe subdirs/files exist without requiring re-init.
        try:
            ensure_vibe_dirs(repo_root, agent_ids=list(self.config.agents.keys()))
        except Exception:
            pass

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

    def _tooling_probe(self) -> tuple[Optional[str], str, list[str], list[str]]:
        """
        Probe local tool availability (PATH detection) so upstream agents don't choose
        architectures that require missing global CLIs (e.g. hugo, docker).

        Returns: (artifact_pointer, summary, available, missing)
        """
        bins = [
            "git",
            "python",
            "pip",
            "node",
            "npm",
            "pnpm",
            "yarn",
            "hugo",
            "docker",
            "psql",
            "sqlite3",
        ]
        found: dict[str, str] = {}
        for b in bins:
            try:
                p = shutil.which(b) or ""
            except Exception:
                p = ""
            found[b] = p

        available = [b for b in bins if found.get(b)]
        missing = [b for b in bins if not found.get(b)]
        summary = f"available={', '.join(available[:8]) or 'none'}; missing={', '.join(missing[:8]) or 'none'}"

        ptr: Optional[str] = None
        try:
            ptr = (
                self.artifacts.put_json(
                    {
                        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "platform": platform.platform(),
                        "bins": bins,
                        "found": found,
                        "available": available,
                        "missing": missing,
                    },
                    suffix=".tooling.json",
                    kind="tooling_probe",
                ).to_pointer()
            )
        except Exception:
            ptr = None

        return ptr, summary, available, missing

    def _write_workspace_contract(
        self,
        *,
        route_level: packs.RouteLevel,
        style: str,
        tooling_ptr: Optional[str],
        tooling_available: list[str],
        tooling_missing: list[str],
    ) -> tuple[Optional[str], str, str]:
        """
        Produce a deterministic, pointer-backed "workspace contract" so agents have a stable
        source of truth about repo layout and runnable commands.

        Writes: .vibe/manifests/workspace_contract.json
        Returns: (read_file_pointer, short_summary, excerpt_text)
        """

        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        mdir = self.repo_root / ".vibe" / "manifests"
        mdir.mkdir(parents=True, exist_ok=True)

        node_dirs = self._find_node_project_dirs()
        node_projects: list[dict[str, Any]] = []
        for d in node_dirs[:8]:
            pkg_path = (self.repo_root / d / "package.json").resolve()
            if not pkg_path.exists():
                continue
            try:
                pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                pkg = {}
            scripts = dict((pkg.get("scripts") or {}) if isinstance(pkg, dict) else {})
            pm = self._package_manager(d)
            rel_pkg = pkg_path.relative_to(self.repo_root).as_posix()
            node_projects.append(
                {
                    "dir": d.as_posix() if d.as_posix() not in {"", "."} else ".",
                    "package_manager": pm,
                    "package_json": rel_pkg,
                    "scripts": {k: str(v) for k, v in list(scripts.items())[:40] if str(k).strip()},
                }
            )

        langs: list[str] = []
        if (self.repo_root / "pyproject.toml").exists() or (self.repo_root / "requirements.txt").exists() or (self.repo_root / "setup.py").exists():
            langs.append("python")
        if node_projects:
            langs.append("node")
        if (self.repo_root / "go.mod").exists():
            langs.append("go")
        if (self.repo_root / "Cargo.toml").exists():
            langs.append("rust")

        # Verification commands are derived from the same heuristics QA uses.
        qa_smoke = self._determine_test_commands(profile="smoke")
        qa_unit = self._determine_test_commands(profile="unit")
        qa_full = self._determine_test_commands(profile="full")

        # Dev/start commands: best-effort, script-driven (facts only; no invention).
        dev_cmds: list[str] = []
        start_cmds: list[str] = []
        install_cmds: list[str] = []
        for np in node_projects[:8]:
            rel = str(np.get("dir") or ".").strip() or "."
            pm = str(np.get("package_manager") or "npm").strip() or "npm"
            scripts = np.get("scripts") or {}
            if isinstance(scripts, dict):
                if "dev" in scripts:
                    dev_cmds.append(self._shell_cmd_in_dir(rel_dir=Path(rel), cmd=f"{pm} run dev"))
                if "start" in scripts:
                    start_cmds.append(self._shell_cmd_in_dir(rel_dir=Path(rel), cmd=f"{pm} start"))
            install_cmds.append(self._shell_cmd_in_dir(rel_dir=Path(rel), cmd=f"{pm} install"))

        # Env templates (existence only; do not read secrets).
        env_files: list[str] = []
        try:
            patterns = [
                ".env.example",
                ".env.template",
                ".env.sample",
                ".env.local.example",
                ".env.development.example",
                ".env.production.example",
            ]
            seen: set[str] = set()
            for pat in patterns:
                for p in self.repo_root.rglob(pat):
                    try:
                        rel = p.relative_to(self.repo_root).as_posix()
                    except Exception:
                        continue
                    if rel.startswith(".vibe/") or rel.startswith(".git/"):
                        continue
                    if rel in seen:
                        continue
                    seen.add(rel)
                    env_files.append(rel)
                    if len(env_files) >= 20:
                        break
                if len(env_files) >= 20:
                    break
        except Exception:
            env_files = []

        contract = {
            "version": 1,
            "generated_at": ts,
            "route_level": route_level,
            "style": str(style or "").strip() or "balanced",
            "languages": langs,
            "tooling_probe": {
                "pointer": tooling_ptr,
                "available": list(tooling_available or []),
                "missing": list(tooling_missing or []),
            },
            "node_projects": node_projects,
            "commands": {
                "setup": install_cmds[:8],
                "dev": dev_cmds[:6],
                "start": start_cmds[:6],
                "qa_smoke": qa_smoke[:10],
                "qa_unit": qa_unit[:14],
                "qa_full": qa_full[:20],
            },
            "env_templates": env_files,
            "notes": [
                "事实源：package.json/lockfiles/现有脚本；不要凭空发明命令或文件。",
                "QA 会基于 qa_* 命令执行；若为空，需先补齐工程骨架/脚本。",
            ],
        }

        path = mdir / "workspace_contract.json"
        try:
            path.write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception:
            return None, "", ""

        try:
            rr = self.toolbox.read_file(agent_id="router", path=".vibe/manifests/workspace_contract.json", start_line=1, end_line=260)
            summary = (
                f"langs={','.join(langs) or 'unknown'}; node_projects={len(node_projects)}; "
                f"qa_full_cmds={len(qa_full)}; env_templates={len(env_files)}"
            )
            return rr.pointer, summary, rr.content or ""
        except Exception:
            return None, "", ""

    def _compute_fix_loop_max_loops(
        self,
        *,
        base_max_loops: int,
        route_level: packs.RouteLevel,
        report: packs.TestReport,
        started_smoke_preflight: bool,
    ) -> int:
        """
        Compute a bounded fix-loop budget.

        Key intuition:
        - If we started with a smoke preflight but require a stronger QA profile (full/integration),
          we must reserve some budget for blockers that only appear in the escalation step
          (e.g. lint/test after build passes).
        """

        max_loops = int(base_max_loops or 3)
        if route_level in {"L3", "L4"}:
            max_loops = max(max_loops, 6)
        try:
            n_blockers = len([b for b in (report.blockers or []) if str(b).strip()])
            if n_blockers > 1:
                max_loops = max(max_loops, min(12, 2 + n_blockers))
        except Exception:
            pass

        if started_smoke_preflight:
            # Reserve a couple extra loops for blockers that only surface when we
            # escalate from smoke (usually build-only) to full verification (lint/test).
            max_loops = max_loops + 2

        return max(1, min(max_loops, 16))

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

    def _collect_user_hints(self, *, task_evt: LedgerEvent, limit: int = 8) -> tuple[list[str], list[str]]:
        """
        Collect user-provided hints/constraints that belong to the given task.

        We treat hints as *requirements constraints*, not repo facts; they are still
        persisted and pointer-backed for auditability.
        """
        if limit <= 0:
            return [], []

        target_ledger = self.main_ledger if (task_evt.branch_id or "main") == "main" else self.ledger
        try:
            events = list(target_ledger.iter_events())
        except Exception:
            return [], []

        start_idx = -1
        for i, e in enumerate(events):
            if e.id == task_evt.id:
                start_idx = i
                break
        if start_idx < 0:
            return [], []

        end_idx = len(events)
        for j in range(start_idx + 1, len(events)):
            if events[j].type == "REQ_CREATED":
                end_idx = j
                break

        hints: list[str] = []
        pointers: list[str] = []
        seen: set[str] = set()
        for e in events[start_idx + 1 : end_idx]:
            if e.type not in {"USER_HINT_ADDED", "REQ_UPDATED"}:
                continue
            raw = e.meta.get("text") if isinstance(e.meta, dict) else None
            txt = str(raw or e.summary or "").strip()
            if not txt:
                continue
            txt = txt.replace("\r", " ").strip()
            if len(txt) > 400:
                txt = txt[:400] + "…（已截断）…"
            if txt in seen:
                continue
            seen.add(txt)
            hints.append(txt)
            for p in list(e.pointers or [])[:4]:
                ps = str(p).strip()
                if ps and ps not in pointers:
                    pointers.append(ps)
            if len(hints) >= limit:
                break

        return hints, pointers

    def _build_context_packet(self, *, task_evt: Optional[LedgerEvent] = None) -> tuple[packs.ContextPacket, str]:
        pointers: List[str] = []
        excerpts: List[str] = []

        def add_repo_excerpt(rel: str, *, end_line: int = 200, include_excerpt: bool = True) -> None:
            path = self.repo_root / rel
            if not path.exists():
                return
            rr = self.toolbox.read_file(agent_id="router", path=rel, start_line=1, end_line=end_line)
            pointers.append(rr.pointer)
            if include_excerpt:
                excerpts.append(f"<<< {rr.pointer} >>>\n{rr.content}\n")

        excerpt_budget = 8

        for rel in [
            ".vibe/manifests/project_manifest.md",
            ".vibe/manifests/run_manifest.md",
            ".vibe/manifests/repo_overview.md",
            "README.md",
        ]:
            try:
                add_repo_excerpt(rel, end_line=200, include_excerpt=(len(excerpts) < excerpt_budget))
            except Exception:
                # best-effort; context snippets are helpful but should not block the workflow
                continue

        # Grounding: include build/test/config “source of truth” snippets so coders don't invent scripts/configs.
        # Keep excerpts small and bounded; pointers are still recorded even when excerpt budget is exceeded.
        try:
            node_dirs = self._find_node_project_dirs()
            if node_dirs:
                candidate_files: list[str] = []
                for d in node_dirs[:4]:
                    prefix = "" if d.as_posix() in {"", "."} else f"{d.as_posix().rstrip('/')}/"
                    candidate_files.extend(
                        [
                            f"{prefix}package.json",
                            f"{prefix}tsconfig.json",
                            f"{prefix}eslint.config.js",
                            f"{prefix}eslint.config.cjs",
                            f"{prefix}.eslintrc",
                            f"{prefix}.eslintrc.json",
                            f"{prefix}.eslintrc.js",
                            f"{prefix}.eslintrc.cjs",
                            f"{prefix}jest.config.js",
                            f"{prefix}jest.config.ts",
                            f"{prefix}vitest.config.ts",
                            f"{prefix}vite.config.ts",
                            f"{prefix}tsconfig.eslint.json",
                        ]
                    )
                # Root-level configs (monorepos / shared config).
                candidate_files.extend(
                    [
                        "package.json",
                        "tsconfig.json",
                        "eslint.config.js",
                        "eslint.config.cjs",
                        ".eslintrc",
                        ".eslintrc.json",
                        ".eslintrc.js",
                        ".eslintrc.cjs",
                        "jest.config.js",
                        "jest.config.ts",
                        "vitest.config.ts",
                        "vite.config.ts",
                        "pnpm-workspace.yaml",
                        ".npmrc",
                    ]
                )

                seen: set[str] = set()
                for rel in candidate_files:
                    r = rel.replace("\\", "/").lstrip("/")
                    if not r or r in seen:
                        continue
                    seen.add(r)
                    try:
                        add_repo_excerpt(r, end_line=220, include_excerpt=(len(excerpts) < excerpt_budget))
                    except Exception:
                        continue
        except Exception:
            # best-effort
            pass

        recent = []
        for evt in self.ledger.iter_events(limit=20, reverse=True):
            recent.append(packs.ContextEventRef(id=evt.id, summary=evt.summary, pointers=evt.pointers))
        ctx = packs.ContextPacket(repo_pointers=pointers, recent_events=recent)

        # Deterministic scaffold hint: if we can't detect any runnable build/test configuration,
        # tell Router/Coder to bootstrap a minimal project skeleton first.
        try:
            has_py = (self.repo_root / "pyproject.toml").exists() or (self.repo_root / "setup.py").exists()
            has_node = bool(self._find_node_project_dirs())
            has_go = (self.repo_root / "go.mod").exists()
            has_rust = (self.repo_root / "Cargo.toml").exists()
            if not (has_py or has_node or has_go or has_rust):
                ctx.constraints.append(
                    "工程骨架提示：当前仓库未检测到可运行的 build/lint/test 配置（如 package.json/pyproject.toml）。"
                    "若目标是从 0 搭建项目，请优先生成工程骨架与最小可验证命令，再继续实现功能。"
                )
        except Exception:
            pass

        if task_evt is not None:
            try:
                hints, hint_ptrs = self._collect_user_hints(task_evt=task_evt, limit=8)
            except Exception:
                hints, hint_ptrs = [], []
            if hints:
                # These are high-priority constraints coming directly from the user.
                for h in hints[:8]:
                    ctx.constraints.append(f"用户提示（高优先级约束）：{h}")
            if hint_ptrs:
                ctx.log_pointers.extend(hint_ptrs[:16])

        # Delivery guardrails (deterministic, tool-driven): reduce "QA discovers everything" churn by grounding early.
        if self._find_node_project_dirs():
            ctx.constraints.append(
                "实现约束：在修改/新增 Node/TS 代码前，先以 repo_pointers 中的 package.json / tsconfig / eslint / test 配置为事实源；"
                "不要发明脚本/路径/依赖。新增 import 必须同步更新对应 package.json 依赖。"
            )

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

    def _contains_any(self, text: str, needles: List[str]) -> bool:
        t = text or ""
        for n in needles:
            if n and n in t:
                return True
        return False

    def _agent_capabilities(self, agent_id: str) -> set[str]:
        cfg = self.config.agents.get(agent_id)
        caps = list(getattr(cfg, "capabilities", []) or []) if cfg is not None else []
        out: set[str] = set()
        for c in caps:
            s = str(c).strip().lower()
            if s:
                out.add(s)
        return out

    def _api_key_available_for_agent(self, agent_id: str) -> bool:
        cfg = self.config.agents.get(agent_id)
        if cfg is None:
            return False
        prov = self.config.providers.get(cfg.provider)
        if prov is None:
            return False
        env = getattr(prov, "api_key_env", None)
        if not env:
            return True
        return bool(os.getenv(str(env)))

    def _select_primary_coder(self, *, task_text: str, risks: RiskSignals, activated_agents: Set[str]) -> str:
        # Prefer integrator for cross-module changes when available.
        if risks.cross_module and "integration_engineer" in activated_agents:
            return "integration_engineer"

        # Prefer frontend coder for clearly-frontend tasks (when available).
        if "coder_frontend" in activated_agents:
            front_words = ["前端", "UI", "界面", "React", "Vite", "TypeScript", "TSX", "组件", "页面"]
            back_words = ["后端", "接口", "API", "数据库", "迁移", "服务端", "Server", "Express", "FastAPI"]
            if self._contains_any(task_text, front_words) and not self._contains_any(task_text, back_words):
                return "coder_frontend"

        return "coder_backend"

    def _select_fix_coder_for_tests(self, *, report: packs.TestReport, blocker_text: str, activated_agents: Set[str]) -> str:
        text = (blocker_text or "").lower()
        cmd = ""
        cmd_dir = Path(".")
        try:
            for r in report.results:
                if not r.passed:
                    cmd = (r.command or "").lower()
                    cmd_dir = self._shell_cd_dir(r.command or "")
                    break
        except Exception:
            cmd = ""
        combined = (cmd + "\n" + text).lower()

        # Prefer routing by the failing command's working directory when possible.
        # This avoids misclassifying backend TypeScript builds as "frontend" just because `tsc` appears.
        cmd_dir_key = cmd_dir.as_posix().replace("\\", "/").strip().lower()
        if cmd_dir_key and cmd_dir_key != ".":
            top = cmd_dir.parts[0].lower() if cmd_dir.parts else ""
            if top in {"client", "frontend", "web", "app"} and "coder_frontend" in activated_agents:
                return "coder_frontend"
            if top in {"backend", "server", "api"}:
                return "coder_backend"

        front = any(
            k in combined
            for k in [
                ".tsx",
                ".jsx",
                "vite",
                "react",
                "eslint",
                "client/",
                "client\\",
                "frontend/",
                "web/",
            ]
        )
        back = any(
            k in combined
            for k in [
                "pytest",
                "unittest",
                "backend/",
                "backend\\",
                "server/",
                "api/",
                "express",
                "prisma",
                "knex",
            ]
        )

        if front and back and "integration_engineer" in activated_agents:
            return "integration_engineer"
        if front and "coder_frontend" in activated_agents:
            return "coder_frontend"
        return "coder_backend"

    def _select_fix_coder_for_review(self, *, review: Optional[packs.ReviewReport], activated_agents: Set[str]) -> str:
        text = " ".join(list(review.blockers or []) if review is not None else []).lower()
        if "integration_engineer" in activated_agents and any(k in text for k in ["contract", "契约", "route", "router", "mismatch", "integration", "兼容"]):
            return "integration_engineer"
        if "coder_frontend" in activated_agents and any(k in text for k in ["tsx", "react", "frontend", "vite"]):
            return "coder_frontend"
        return "coder_backend"

    def _select_fix_coder_for_text(self, *, text: str, activated_agents: Set[str]) -> str:
        combined = (text or "").lower()
        if "integration_engineer" in activated_agents and any(
            k in combined for k in ["contract", "契约", "route", "router", "mismatch", "integration", "兼容"]
        ):
            return "integration_engineer"
        front = any(k in combined for k in ["tsx", "jsx", "react", "frontend", "vite", "client/", "client\\", "eslint"])
        back = any(k in combined for k in ["backend/", "backend\\", "server/", "api/", "express", "prisma", "knex", "pytest", "unittest"])
        if front and back and "integration_engineer" in activated_agents:
            return "integration_engineer"
        if front and "coder_frontend" in activated_agents:
            return "coder_frontend"
        return "coder_backend"

    def _agent_pool_for_route(self, route_level: packs.RouteLevel) -> list[str]:
        """
        Agent pool is a *preference list* (configurable). It is NOT a hard restriction:
        required gates may pull in additional agents, and incident-driven routing may
        consult extra agents when needed.
        """

        profile = (self.config.routes.levels or {}).get(route_level)
        agents = list(profile.agents) if profile else []
        if not agents:
            # Backward compatible fallback: treat as L1 minimal set.
            agents = ["pm", "router", "coder_backend", "qa"] if route_level != "L0" else ["router", "coder_backend", "qa"]

        # Router/coder/qa are baseline; keep them in pool so on-demand activation is possible.
        baseline = ["router", "coder_backend", "qa"]
        if route_level != "L0":
            baseline.insert(1, "pm")
        for b in baseline:
            if b in self.config.agents and b not in agents:
                agents.insert(0, b) if b == "router" else agents.append(b)

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

    def _required_agents_for_route(self, route_level: packs.RouteLevel, *, risks: RiskSignals) -> list[str]:
        """
        Minimal activation set for the workflow start.

        We intentionally keep this small and activate additional agents *on-demand* right
        before the gate that needs them. This makes the system less "flowchart-ish" while
        keeping hard gates auditable (each activation is logged).
        """

        req: list[str] = []

        def add(x: str) -> None:
            if x in self.config.agents and x not in req:
                req.append(x)

        # Router is mandatory for ledger/gates.
        add("router")
        add("coder_backend")
        add("qa")

        if route_level != "L0":
            add("pm")

        return req

    def _append_guarded(self, *, event: LedgerEvent, activated_agents: Set[str]) -> None:
        if event.agent != "user" and event.agent not in activated_agents:
            raise RuntimeError(f"Agent not activated for this route: {event.agent}")
        agent_cfg = self.config.agents.get(event.agent)
        if agent_cfg and agent_cfg.memory_scope.ledger_write_types:
            allowed = set(agent_cfg.memory_scope.ledger_write_types)
            if event.type not in allowed:
                raise RuntimeError(f"Ledger write type not allowed for agent {event.agent}: {event.type}")
        self.ledger.append(event)
        try:
            self._cc_implementation_lead(event)
        except Exception:
            pass

    def _cc_implementation_lead(self, event: LedgerEvent) -> None:
        """
        Always-on "CC" channel for the implementation lead.

        Goal: even when we don't actively consult the lead for small changes, they should
        still have a pointer-backed, structured trace of what coders/QA/router did. This
        helps prevent "各自为政" drift and enables fast escalation when issues grow.

        This is intentionally best-effort and compact (no long logs, only pointers).
        """
        if "implementation_lead" not in self.config.agents:
            return

        if (event.agent or "").strip() in {"implementation_lead"}:
            return

        t = str(event.type or "").strip()
        if not t:
            return

        # Keep signal high: only CC key workflow events.
        interesting = {
            "ROUTE_SELECTED",
            "PLAN_CREATED",
            "LEAD_BLUEPRINT_BUILT",
            "CONTEXT_PACKET_BUILT",
            "WORKSPACE_CONTRACT_BUILT",
            "ENV_PROBED",
            "ENV_UPDATED",
            "PATCH_WRITTEN",
            "CODE_COMMIT",
            "CODE_REFACTOR",
            "TEST_RUN",
            "TEST_PASSED",
            "TEST_FAILED",
            "INCIDENT_CREATED",
            "REVIEW_PASSED",
            "REVIEW_BLOCKED",
            "SEC_REVIEW_PASSED",
            "SEC_REVIEW_BLOCKED",
            "CHECKPOINT_CREATED",
        }
        if t not in interesting:
            return

        kind = "strategy"
        if t in {"TEST_FAILED", "INCIDENT_CREATED", "REVIEW_BLOCKED", "SEC_REVIEW_BLOCKED"}:
            kind = "incident"
        elif t == "CHECKPOINT_CREATED":
            kind = "postmortem"

        meta = event.meta if isinstance(event.meta, dict) else {}

        def _take_str(x: Any, *, limit: int = 240) -> str:
            s = str(x or "").strip().replace("\r", "").replace("\t", " ")
            if len(s) > limit:
                s = s[:limit] + "…"
            return s

        pinned: list[str] = []
        pinned.append(f"event={event.id}")
        if meta.get("route_level"):
            pinned.append(f"route_level={_take_str(meta.get('route_level'), limit=40)}")
        if meta.get("style"):
            pinned.append(f"style={_take_str(meta.get('style'), limit=40)}")
        if meta.get("phase"):
            pinned.append(f"phase={_take_str(meta.get('phase'), limit=40)}")
        if meta.get("loop") is not None:
            pinned.append(f"loop={_take_str(meta.get('loop'), limit=20)}")

        files = meta.get("files_changed")
        if isinstance(files, list) and files:
            shown = ", ".join([_take_str(x, limit=80) for x in list(files)[:8] if str(x).strip()]).strip()
            if shown:
                pinned.append(f"files={shown}" + (" …" if len(files) > 8 else ""))

        cmds = meta.get("commands")
        if isinstance(cmds, list) and cmds:
            shown = " | ".join([_take_str(x, limit=120) for x in list(cmds)[:2] if str(x).strip()]).strip()
            if shown:
                pinned.append(f"cmds={shown}" + (" …" if len(cmds) > 2 else ""))

        blocker = meta.get("blocker")
        if isinstance(blocker, str) and blocker.strip():
            pinned.append("blocker=" + _take_str(self._compact_error_excerpt(blocker, max_lines=18, max_chars=320), limit=320))

        summary = f"{event.agent} {t}: {str(event.summary or '').strip()}".strip()
        summary = _take_str(summary, limit=220)

        view_dir = self.repo_root / ".vibe" / "views" / "implementation_lead"
        mem_path = view_dir / "memory.jsonl"
        append_memory_record(
            mem_path,
            MemoryRecord(
                ts=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                agent_id="implementation_lead",
                kind=kind,  # type: ignore[arg-type]
                digest=ChatDigest(summary=summary, pinned=pinned[:8], background=[], open_questions=[]),
                pointers=list(event.pointers or [])[:24],
            ),
        )

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
            kind = str(getattr(r, "kind", "") or "").strip()
            prefix = f"({kind}) " if kind and kind != "chat_digest" else ""
            lines.append(f"- {prefix}{r.digest.summary.strip()[:200]}")
            if pin:
                lines.append(f"  要点: {pin}")
            if ptrs:
                lines.append(f"  pointers: {ptrs}")
        return "\n".join(lines).strip()

    def _tokenize_for_similarity(self, text: str) -> set[str]:
        t = (text or "").lower()
        toks = re.findall(r"[a-z0-9][a-z0-9_./:-]{2,}", t)
        return set(toks)

    def _similar_lessons_for_query(self, *, agent_id: str, query: str, limit: int = 3) -> list[MemoryRecord]:
        view_dir = self.repo_root / ".vibe" / "views" / agent_id
        mem_path = view_dir / "memory.jsonl"
        if not mem_path.exists():
            return []

        try:
            recs = read_memory_records(mem_path, limit=600)
        except Exception:
            return []

        lessons = [r for r in recs if str(getattr(r, "kind", "") or "") == "lesson"]
        if not lessons:
            return []

        q = (query or "").strip()
        if not q:
            return []
        q_tokens = self._tokenize_for_similarity(q)

        scored: list[tuple[float, MemoryRecord]] = []
        for r in lessons:
            text = (r.digest.summary or "") + "\n" + "\n".join(list(r.digest.pinned or [])[:6])
            tokens = self._tokenize_for_similarity(text)
            if q_tokens and tokens:
                jacc = len(q_tokens & tokens) / max(1, len(q_tokens | tokens))
            else:
                jacc = 0.0
            try:
                seq = difflib.SequenceMatcher(None, q[:2000].lower(), text[:2000].lower()).ratio()
            except Exception:
                seq = 0.0
            score = (jacc * 0.75) + (seq * 0.25)
            if score < 0.06:
                continue
            scored.append((score, r))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _s, r in scored[: max(0, int(limit))]]

    def _format_lessons_for_prompt(self, recs: list[MemoryRecord]) -> str:
        if not recs:
            return ""
        lines: list[str] = []
        lines.append("SimilarLessons（过往修复经验；事实以 pointers 展开为准）：")
        for r in recs[:4]:
            pin = "；".join([str(x).strip() for x in (r.digest.pinned or []) if str(x).strip()][:3]).strip()
            ptrs = ", ".join([str(p).strip() for p in (r.pointers or []) if str(p).strip()][:2]).strip()
            s = f"- {str(r.digest.summary or '').strip()[:200]}"
            if pin:
                s = s + f"\n  要点: {pin[:240]}"
            if ptrs:
                s = s + f"\n  pointers: {ptrs}"
            lines.append(s)
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

    def _find_node_project_dirs(self) -> list[Path]:
        """
        Return relative directories that contain a package.json.
        Ordered by common full-stack conventions (backend/server before client/frontend).
        """
        found: list[Path] = []

        root_pkg = self.repo_root / "package.json"
        if root_pkg.exists():
            found.append(Path("."))

        # Common layouts (prefer backend-ish first so API compiles before UI).
        for rel in ["backend", "server", "api", "client", "frontend", "web", "app"]:
            if (self.repo_root / rel / "package.json").exists():
                found.append(Path(rel))

        # Best-effort shallow search (avoid scanning large repos).
        try:
            for p in self.repo_root.rglob("package.json"):
                rel = p.relative_to(self.repo_root)
                if rel.parts and rel.parts[0] in {".git", ".vibe", "node_modules", "dist", "build"}:
                    continue
                if len(rel.parts) <= 2:
                    found.append(rel.parent)
        except Exception:
            pass

        out: list[Path] = []
        seen: set[str] = set()
        for d in found:
            key = d.as_posix()
            if not key:
                key = "."
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
        return out

    def _find_node_project_dir(self) -> Optional[Path]:
        """Backward compatible: return the first Node project dir, if any."""
        dirs = self._find_node_project_dirs()
        return dirs[0] if dirs else None

    def _node_has_tests(self, node_dir: Path) -> bool:
        root = self.repo_root / node_dir
        for d in ["test", "tests", "__tests__"]:
            if (root / d).exists():
                return True
        try:
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                rel = p.relative_to(root)
                if rel.parts and rel.parts[0] in {"node_modules", "dist", "build"}:
                    continue
                name = p.name.lower()
                if name.endswith(
                    (
                        ".test.ts",
                        ".test.tsx",
                        ".test.js",
                        ".test.jsx",
                        ".spec.ts",
                        ".spec.tsx",
                        ".spec.js",
                        ".spec.jsx",
                    )
                ):
                    return True
        except Exception:
            return False
        return False

    def _shell_cd_dir(self, cmd: str) -> Path:
        c = cmd or ""
        # Our own helper always uses quotes; handle both Windows and POSIX variants.
        m = re.match(r'^\s*cd\s+(?:/d\s+)?\"(?P<dir>[^\"]+)\"\s*&&', c, flags=re.IGNORECASE)
        if m:
            return Path(m.group("dir"))
        return Path(".")

    def _node_lockfile(self, node_dir: Path, pm: str) -> Path:
        root = self.repo_root / node_dir
        if pm == "pnpm":
            return root / "pnpm-lock.yaml"
        if pm == "yarn":
            return root / "yarn.lock"
        return root / "package-lock.json"

    def _node_install_needed(self, node_dir: Path) -> tuple[bool, str]:
        root = self.repo_root / node_dir
        pkg_path = root / "package.json"
        if not pkg_path.exists():
            return False, ""
        pm = self._package_manager(node_dir)
        lock = self._node_lockfile(node_dir, pm)
        node_modules = root / "node_modules"
        if not node_modules.exists():
            return True, "node_modules_missing"
        if not lock.exists():
            return True, "lockfile_missing"
        try:
            if pkg_path.stat().st_mtime > lock.stat().st_mtime:
                return True, "package_json_newer_than_lockfile"
        except Exception:
            # If stat fails, fall back to dependency probing.
            pass

        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
            deps: dict[str, str] = {}
            for k in ["dependencies", "devDependencies", "optionalDependencies"]:
                deps.update(dict(pkg.get(k) or {}))
            for dep in deps.keys():
                parts = [p for p in str(dep).split("/") if p]
                if not parts:
                    continue
                if not (node_modules.joinpath(*parts)).exists():
                    return True, f"missing_dep:{dep}"
        except Exception:
            # Can't parse package.json; assume install not needed.
            return False, ""

        return False, ""

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

    def _node_external_pkg_name(self, spec: str) -> Optional[str]:
        s = (spec or "").strip()
        if not s:
            return None
        if s.startswith(("node:", "bun:")):
            return None
        if s.startswith((".", "/", "\\")):
            return None
        # Common TS path aliases.
        if s.startswith(("@/", "#/", "~/")):
            return None
        # Strip query/hash fragments.
        s = s.split("?", 1)[0].split("#", 1)[0].strip()
        if not s:
            return None

        # Scoped packages keep '@scope/name'.
        if s.startswith("@"):
            parts = [p for p in s.split("/") if p]
            if len(parts) >= 2:
                return "/".join(parts[:2])
            return s
        return s.split("/", 1)[0]

    def _doctor_node_missing_deps(
        self,
        *,
        node_dir: Path,
        declared: set[str],
        max_packages: int = 4,
        max_files: int = 80,
    ) -> list[dict[str, Any]]:
        """
        Static scan: find external import packages that are not declared in package.json.

        Returns a small list of findings with evidence pointers (best-effort).
        """
        base = (self.repo_root / node_dir).resolve()
        if not base.exists():
            return []

        # Node built-ins (subset; good enough to avoid common false positives).
        builtins = {
            "assert",
            "buffer",
            "child_process",
            "crypto",
            "events",
            "fs",
            "http",
            "https",
            "net",
            "os",
            "path",
            "stream",
            "timers",
            "tls",
            "url",
            "util",
            "zlib",
        }

        # Prefer scanning src/, but fall back to repo root of the node project.
        scan_root = base / "src"
        if not scan_root.exists():
            scan_root = base

        import_re = re.compile(
            r"(?:\bimport\b[\s\S]{0,120}?\bfrom\s*[\"'](?P<a>[^\"']+)[\"']|\bexport\b[\s\S]{0,120}?\bfrom\s*[\"'](?P<b>[^\"']+)[\"']|\brequire\s*\(\s*[\"'](?P<c>[^\"']+)[\"']\s*\)|\bimport\s*\(\s*[\"'](?P<d>[^\"']+)[\"']\s*\))",
            flags=re.IGNORECASE,
        )

        findings: list[dict[str, Any]] = []
        seen_pkg: set[str] = set()

        def should_skip(path: Path) -> bool:
            try:
                rel = path.relative_to(base)
            except Exception:
                return True
            if rel.parts and rel.parts[0] in {"node_modules", "dist", "build", ".vibe", ".git"}:
                return True
            return False

        count_files = 0
        try:
            for path in scan_root.rglob("*"):
                if count_files >= max_files or len(findings) >= max_packages:
                    break
                if not path.is_file():
                    continue
                if should_skip(path):
                    continue
                if path.suffix.lower() not in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                count_files += 1
                for m in import_re.finditer(text):
                    if len(findings) >= max_packages:
                        break
                    spec = m.group("a") or m.group("b") or m.group("c") or m.group("d") or ""
                    pkg = self._node_external_pkg_name(spec)
                    if not pkg or pkg in seen_pkg:
                        continue
                    if pkg in builtins:
                        continue
                    if pkg in declared:
                        continue
                    seen_pkg.add(pkg)
                    line = text[: m.start()].count("\n") + 1
                    rel_repo = path.relative_to(self.repo_root).as_posix()
                    ptr: Optional[str] = None
                    try:
                        rr = self.toolbox.read_file(
                            agent_id="router",
                            path=rel_repo,
                            start_line=max(1, line - 2),
                            end_line=line + 2,
                        )
                        ptr = rr.pointer
                    except Exception:
                        ptr = None
                    findings.append({"package": pkg, "file": rel_repo, "line": line, "pointer": ptr})
        except Exception:
            return findings

        return findings

    def _doctor_node_bin_shims(self, *, node_dir: Path, max_items: int = 8) -> list[dict[str, Any]]:
        """
        Windows-only: detect suspicious local CLI shims under node_modules/.bin.

        Why: some toolchains create 0-byte `.exe` placeholder shims on Windows. If a repo
        script directly spawns `node_modules/.bin/<tool>.exe`, Node may error with
        `spawn UNKNOWN` (or similar). The safe pattern is to invoke the tool via npm
        scripts (PATH shim) or prefer `<tool>.cmd` on Windows when spawning a path.
        """
        if os.name != "nt":
            return []

        bin_dir = (self.repo_root / node_dir / "node_modules" / ".bin").resolve()
        if not bin_dir.exists() or not bin_dir.is_dir():
            return []

        items: list[dict[str, Any]] = []
        try:
            for exe in list(bin_dir.glob("*.exe"))[:240]:
                try:
                    if not exe.is_file():
                        continue
                    size = int(exe.stat().st_size)
                except Exception:
                    continue
                if size != 0:
                    continue

                tool = exe.stem
                cmd = bin_dir / f"{tool}.cmd"
                ps1 = bin_dir / f"{tool}.ps1"
                try:
                    rel_exe = exe.relative_to(self.repo_root).as_posix()
                except Exception:
                    rel_exe = exe.as_posix()
                items.append(
                    {
                        "tool": tool,
                        "exe": rel_exe,
                        "size": size,
                        "cmd_exists": bool(cmd.exists()),
                        "ps1_exists": bool(ps1.exists()),
                    }
                )
                if len(items) >= max(1, int(max_items)):
                    break
        except Exception:
            return []

        if not items:
            return []

        tools = ", ".join([str(x.get("tool") or "").strip() for x in items if str(x.get("tool") or "").strip()][:6])
        detail = (
            "发现 0 字节 `.exe` shim：在 Windows 下，如果脚本直接执行 "
            "`node_modules/.bin/<tool>.exe`，可能触发 `spawn UNKNOWN`/`ENOENT` 等错误。"
            "建议：通过 `npm run <script>` 调用（让 PATH shim 生效），或在 spawn 时优先使用 `<tool>.cmd`，"
            "并避免硬编码 `.exe` 路径。"
        )
        return [
            {
                "kind": "node_bin_zero_byte_exe",
                "severity": "high",
                "title": f"node_modules/.bin 存在 0 字节 .exe shim（Windows spawn 可能失败）：{tools}",
                "detail": detail[:400],
                "pointers": [],
                "items": items,
            }
        ]

    def _doctor_node_scripts_bin_usage(self, *, node_dir: Path, max_files: int = 14) -> list[dict[str, Any]]:
        """
        Detect repo scripts that directly reference `node_modules/.bin` (especially `.exe`),
        which is a common cross-platform pitfall.
        """
        findings: list[dict[str, Any]] = []
        roots: list[Path] = []
        # Prefer scanning explicit scripts/ folders (avoid rglob'ing the entire repo).
        for r in [self.repo_root / node_dir / "scripts", self.repo_root / "scripts"]:
            if r.exists() and r.is_dir():
                roots.append(r)

        if not roots:
            return findings

        pat_bin = re.compile(r"node_modules[\\/]\.bin", flags=re.IGNORECASE)
        pat_bin_exe = re.compile(r"node_modules[\\/]\.bin[\\/][A-Za-z0-9_.-]+\.exe\b", flags=re.IGNORECASE)

        checked = 0
        for root in roots[:3]:
            try:
                for p in root.rglob("*"):
                    if checked >= max(1, int(max_files)):
                        break
                    if not p.is_file():
                        continue
                    if p.suffix.lower() not in {".js", ".cjs", ".mjs", ".ts"}:
                        continue
                    try:
                        text = p.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    if not text:
                        continue
                    if len(text) > 200_000:
                        text = text[:200_000]
                    m = pat_bin.search(text)
                    if not m:
                        continue
                    checked += 1

                    line = (text[: m.start()].count("\n") + 1) if m.start() >= 0 else 1
                    try:
                        rel_repo = p.relative_to(self.repo_root).as_posix()
                    except Exception:
                        rel_repo = p.as_posix()

                    ptr: Optional[str] = None
                    try:
                        rr = self.toolbox.read_file(
                            agent_id="router",
                            path=rel_repo,
                            start_line=max(1, line - 2),
                            end_line=line + 3,
                        )
                        ptr = rr.pointer
                    except Exception:
                        ptr = None

                    severity = "medium"
                    title = f"脚本直接引用 node_modules/.bin：{rel_repo}"
                    if pat_bin_exe.search(text):
                        severity = "high"
                        title = f"脚本直接引用 node_modules/.bin/*.exe（Windows 易踩坑）：{rel_repo}"
                    findings.append(
                        {
                            "kind": "script_uses_node_modules_bin",
                            "severity": severity,
                            "title": title[:200],
                            "detail": (
                                "直接拼接 `.bin` 路径会绕过 npm 的跨平台 shim 机制（Windows 常用 `.cmd`），"
                                "并可能选到不可执行占位文件。建议：用 `npm run` 调用，或在 Windows 上优先 `.cmd`，"
                                "并对可执行文件做 size>0/exists 检查。"
                            ),
                            "pointers": [p for p in [ptr] if p],
                        }
                    )
            except Exception:
                continue

        return findings

    def _node_bin_health_report(self, *, node_dir: Path, max_items: int = 20) -> tuple[Optional[str], str]:
        """
        Build a small, pointer-backed report about node_modules/.bin health.

        This is used when we suspect an environment/tooling issue (e.g. repeated `spawn UNKNOWN`).
        """
        bin_dir = (self.repo_root / node_dir / "node_modules" / ".bin").resolve()
        if not bin_dir.exists() or not bin_dir.is_dir():
            return None, ""

        suspicious: list[dict[str, Any]] = []
        try:
            for p in list(bin_dir.iterdir())[:1200]:
                if not p.is_file():
                    continue
                name = p.name
                low = name.lower()
                if not (low.endswith(".exe") or low.endswith(".cmd") or low.endswith(".ps1")):
                    continue
                try:
                    size = int(p.stat().st_size)
                except Exception:
                    size = -1

                # Most `.cmd` shims are tiny; only `.exe` with size==0 is a strong smell.
                if low.endswith(".exe") and size == 0:
                    tool = p.stem
                    suspicious.append(
                        {
                            "tool": tool,
                            "file": p.relative_to(self.repo_root).as_posix(),
                            "size": size,
                            "cmd_exists": bool((bin_dir / f"{tool}.cmd").exists()),
                            "ps1_exists": bool((bin_dir / f"{tool}.ps1").exists()),
                        }
                    )
                if len(suspicious) >= max(1, int(max_items)):
                    break
        except Exception:
            suspicious = []

        report = {
            "version": 1,
            "node_dir": node_dir.as_posix() if node_dir.as_posix() not in {"", "."} else ".",
            "bin_dir": bin_dir.relative_to(self.repo_root).as_posix() if bin_dir.is_absolute() else str(bin_dir),
            "suspicious": suspicious,
        }
        ptr = self.artifacts.put_json(report, suffix=".binhealth.json", kind="binhealth").to_pointer()
        tools = ", ".join([str(x.get("tool") or "").strip() for x in suspicious if str(x.get("tool") or "").strip()][:6])
        summary = f"zero_byte_exe={len(suspicious)}" + (f" ({tools})" if tools else "")
        return ptr, summary

    def _doctor_preflight(self, *, max_findings: int = 6) -> tuple[Optional[str], str]:
        """
        Lightweight static preflight to reduce "QA discovers everything" churn.

        Returns (doctor_report_pointer, short_summary).
        """
        node_dirs = self._find_node_project_dirs()
        if not node_dirs:
            return None, ""

        findings: list[dict[str, Any]] = []
        for node_dir in node_dirs[:3]:
            pkg_path = (self.repo_root / node_dir / "package.json").resolve()
            if not pkg_path.exists():
                continue
            try:
                pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue

            scripts = dict(pkg.get("scripts") or {})
            deps: dict[str, str] = dict(pkg.get("dependencies") or {})
            dev_deps: dict[str, str] = dict(pkg.get("devDependencies") or {})
            declared = set(deps.keys()) | set(dev_deps.keys())

            pkg_ptr: Optional[str] = None
            try:
                rel_pkg = pkg_path.relative_to(self.repo_root).as_posix()
                rr = self.toolbox.read_file(agent_id="router", path=rel_pkg, start_line=1, end_line=220)
                pkg_ptr = rr.pointer
            except Exception:
                pkg_ptr = None

            # Windows pitfall: single-quoted globs in npm scripts.
            if os.name == "nt":
                for name, cmd in list(scripts.items())[:30]:
                    s = str(cmd or "")
                    if re.search(r"'[^']*\\*\\*[^']*'", s):
                        findings.append(
                            {
                                "kind": "npm_script_single_quote_glob",
                                "severity": "medium",
                                "title": f"npm script `{name}` 可能在 Windows 下因单引号 glob 失败",
                                "detail": s[:240],
                                "pointers": [p for p in [pkg_ptr] if p],
                            }
                        )
                        break
                    if re.search(r"(^|\\s)[A-Z_]+=[^\\s]+", s) and "cross-env" not in s:
                        findings.append(
                            {
                                "kind": "npm_script_env_assignment",
                                "severity": "low",
                                "title": f"npm script `{name}` 可能在 Windows 下因环境变量写法失败（建议 cross-env）",
                                "detail": s[:240],
                                "pointers": [p for p in [pkg_ptr] if p],
                            }
                        )
                        break

            # Missing deps (static scan).
            missing = self._doctor_node_missing_deps(node_dir=node_dir, declared=declared, max_packages=4, max_files=80)
            for it in missing[: max(0, max_findings - len(findings))]:
                pkg_name = str(it.get("package") or "").strip()
                if not pkg_name:
                    continue
                ptrs = [p for p in [pkg_ptr, it.get("pointer")] if p]
                findings.append(
                    {
                        "kind": "missing_dependency",
                        "severity": "high",
                        "title": f"代码引用了未声明的依赖：{pkg_name}",
                        "detail": f"{it.get('file')}:{it.get('line')}",
                        "pointers": ptrs[:4],
                    }
                )
                if len(findings) >= max_findings:
                    break
            if len(findings) >= max_findings:
                break

            # Windows/Node: suspicious local CLI shims and scripts that hardcode `.bin` paths.
            for f in self._doctor_node_bin_shims(node_dir=node_dir)[: max(0, max_findings - len(findings))]:
                findings.append(f)
                if len(findings) >= max_findings:
                    break
            if len(findings) >= max_findings:
                break
            for f in self._doctor_node_scripts_bin_usage(node_dir=node_dir)[: max(0, max_findings - len(findings))]:
                findings.append(f)
                if len(findings) >= max_findings:
                    break
            if len(findings) >= max_findings:
                break

        if not findings:
            return None, ""

        report = {"version": 1, "scope": "preflight", "findings": findings}
        ptr = self.artifacts.put_json(report, suffix=".doctor.json", kind="doctor").to_pointer()

        titles = [str(f.get("title") or "").strip() for f in findings if str(f.get("title") or "").strip()]
        summary = "；".join(titles[:3])[:200]
        return ptr, summary

    def _artifact_tail_text(self, pointer: str, *, max_bytes: int = 16000) -> str:
        """
        Read the tail of an artifact text file so LLM prompts can include
        concrete failure details without blowing up context windows.
        """
        try:
            rel = str(pointer or "").split("@sha256:", 1)[0].strip()
            if not rel:
                return ""
            abs_path = (self.repo_root / rel).resolve()
            if not abs_path.exists() or not abs_path.is_file():
                return ""
            size = abs_path.stat().st_size
            with abs_path.open("rb") as f:
                if size > max_bytes:
                    f.seek(max(0, size - max_bytes))
                data = f.read(max_bytes)
            text = decode_bytes(data)
            if size > max_bytes:
                return "…（已截断，仅显示末尾）…\n" + text
            return text
        except Exception:
            return ""

    def _artifact_head_text(self, pointer: str, *, max_bytes: int = 16000) -> str:
        """
        Read the head of an artifact text file (useful when errors appear early).
        """
        try:
            rel = str(pointer or "").split("@sha256:", 1)[0].strip()
            if not rel:
                return ""
            abs_path = (self.repo_root / rel).resolve()
            if not abs_path.exists() or not abs_path.is_file():
                return ""
            size = abs_path.stat().st_size
            with abs_path.open("rb") as f:
                data = f.read(max_bytes)
            text = decode_bytes(data)
            if size > max_bytes:
                return text + "\n…（已截断，仅显示开头）…"
            return text
        except Exception:
            return ""

    def _artifact_peek_text(self, pointer: str, *, head_bytes: int = 60000, tail_bytes: int = 60000) -> str:
        """
        Read a bounded "window" of an artifact text file: head + tail (with a marker).

        This increases error-signal coverage vs tail-only, while keeping prompts bounded.
        """
        try:
            rel = str(pointer or "").split("@sha256:", 1)[0].strip()
            if not rel:
                return ""
            abs_path = (self.repo_root / rel).resolve()
            if not abs_path.exists() or not abs_path.is_file():
                return ""
            size = abs_path.stat().st_size
            if size <= max(0, int(head_bytes)) + max(0, int(tail_bytes)):
                return decode_bytes(abs_path.read_bytes())

            head = self._artifact_head_text(pointer, max_bytes=max(0, int(head_bytes)))
            tail = self._artifact_tail_text(pointer, max_bytes=max(0, int(tail_bytes)))
            parts: list[str] = []
            if head.strip():
                parts.append(head.strip())
            parts.append("…（中间省略）…")
            if tail.strip():
                parts.append(tail.strip())
            return "\n".join(parts).strip()
        except Exception:
            return ""

    def _test_failure_excerpt(self, report: packs.TestReport) -> str:
        try:
            for r in report.results:
                if not r.passed:
                    stdout_text = self._artifact_tail_text(r.stdout, max_bytes=12000) if r.stdout else ""
                    stderr_text = self._artifact_tail_text(r.stderr, max_bytes=12000) if r.stderr else ""
                    parts: list[str] = []
                    parts.append(f"FailedCommand: {r.command}")
                    if stdout_text.strip():
                        parts.append("\nSTDOUT:\n" + stdout_text.strip())
                    if stderr_text.strip():
                        parts.append("\nSTDERR:\n" + stderr_text.strip())
                    return "\n".join(parts).strip()
        except Exception:
            return ""
        return ""

    def _failed_command_from_report(self, report: packs.TestReport) -> str:
        try:
            for r in report.results:
                if not r.passed:
                    return str(r.command or "").strip()
        except Exception:
            return ""
        return ""

    def _extract_error_signals(self, text: str, *, limit: int = 14) -> list[str]:
        """
        Extract a small, de-duplicated list of error "signals" from noisy output.

        Used to (a) batch related fixes in a single loop, and (b) anchor lessons.
        """

        t = (text or "").strip()
        if not t:
            return []

        out: list[str] = []
        seen: set[str] = set()

        def add(s: str) -> None:
            s2 = " ".join(str(s or "").strip().split())
            if not s2:
                return
            if len(s2) > 260:
                s2 = s2[:260] + "…"
            key = s2.lower()
            if key in seen:
                return
            seen.add(key)
            out.append(s2)

        # Pytest summary lines.
        for m in re.finditer(r"^(?:ERROR|FAILED)\s+(.+)$", t, flags=re.MULTILINE):
            add(m.group(0))
            if len(out) >= limit:
                return out[:limit]

        # Common Python import/collection failures.
        for m in re.finditer(
            r"^(?:E\s+)?(?:ModuleNotFoundError|ImportError|SyntaxError|NameError|AttributeError|TypeError):\s+.+$",
            t,
            flags=re.MULTILINE,
        ):
            add(m.group(0))
            if len(out) >= limit:
                return out[:limit]

        # TypeScript compiler errors (tsc/vite).
        for m in re.finditer(r"^(?:.+\(\d+,\d+\):\s+error\s+TS\d+:\s+.+)$", t, flags=re.MULTILINE):
            add(m.group(0))
            if len(out) >= limit:
                return out[:limit]

        # Generic "cannot find module" / missing file patterns.
        for m in re.finditer(
            r"^(?:.*)(?:Cannot find module|Module not found|No such file|ENOENT).*$", t, flags=re.MULTILINE
        ):
            add(m.group(0))
            if len(out) >= limit:
                return out[:limit]

        # Fallback: pull a few "error:" lines.
        for m in re.finditer(r"^.*\berror\b.*$", t, flags=re.MULTILINE | re.IGNORECASE):
            add(m.group(0))
            if len(out) >= limit:
                return out[:limit]

        return out[:limit]

    def _failure_signature(self, *, report: packs.TestReport, extracted: list[str], blocker_text: str) -> str:
        """
        A short, stable signature for "did we make progress?" detection.
        """

        cmd = self._failed_command_from_report(report)
        parts: list[str] = []
        if cmd:
            parts.append("cmd:" + " ".join(cmd.strip().split())[:220].lower())
        sigs = extracted or self._extract_error_signals(blocker_text, limit=10)
        for s in [str(x or "") for x in sigs[:10]]:
            s2 = " ".join(s.strip().split()).lower()
            if not s2:
                continue
            parts.append(s2[:220])
        return "|".join(parts)[:1200]

    def _failure_fingerprint(self, *, signature: str) -> str:
        """
        A short fingerprint for de-duplicating repeated failures.

        NOTE: fingerprint is derived from the (already normalized) failure signature; it is
        stable across runs and safe to log in ledger meta.
        """
        s = (signature or "").strip()
        if not s:
            return ""
        h = hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()
        return f"fp_{h[:12]}"

    def _focus_commands_for_test_failure(self, *, report: packs.TestReport, blocker_text: str) -> List[str]:
        """
        Build a minimal verification command list for fix-loop.

        Strategy:
        - Re-run only the *failing command* (avoid re-running earlier passing steps).
        - For pytest failures, try to narrow to failing test files/nodeids or to collection-only.
        """

        failed_cmd = self._failed_command_from_report(report)
        if not failed_cmd:
            return []

        low_cmd = failed_cmd.lower()
        low = (blocker_text or "").lower()

        if "pytest" in low_cmd:
            # If pytest failed during collection/import, `--collect-only` is usually enough.
            collection_smell = any(
                k in low
                for k in [
                    "importerror while importing test module",
                    "modulenotfounderror",
                    "cannot import name",
                    "syntaxerror",
                ]
            )

            nodeids: list[str] = []
            for m in re.finditer(r"\btests[/\\][^\s'\"()]+?\.py::[A-Za-z0-9_:.\\/-]+\b", blocker_text or ""):
                nodeids.append(m.group(0).replace("\\", "/"))

            files: list[str] = []
            for m in re.finditer(r"\btests[/\\][^\s'\"()]+?\.py\b", blocker_text or ""):
                files.append(m.group(0).replace("\\", "/"))

            def uniq(xs: list[str]) -> list[str]:
                out: list[str] = []
                seen: set[str] = set()
                for x in xs:
                    s = str(x or "").strip()
                    if not s:
                        continue
                    if s in seen:
                        continue
                    seen.add(s)
                    out.append(s)
                return out

            nodeids = uniq(nodeids)[:3]
            files = uniq(files)[:3]

            extra: list[str] = []
            if collection_smell:
                extra.append("--collect-only")
            if nodeids:
                extra.extend(nodeids)
            elif files:
                extra.extend(files)

            if extra:
                return [failed_cmd + " " + " ".join(extra)]

        return [failed_cmd]

    def _repo_excerpts_for_test_failure(
        self, report: packs.TestReport, *, max_error_files: int = 4, max_chars: int = 9000
    ) -> str:
        """
        Best-effort: extract relevant repo snippets around compiler/test errors so fix-loop
        coders have concrete file context (with pointers) instead of only logs.
        """
        failed: Optional[packs.TestResult] = None
        for r in report.results:
            if not r.passed:
                failed = r
                break
        if not failed:
            return ""

        workdir = self._shell_cd_dir(failed.command or "")
        # Use a head+tail peek so we can see early "ERROR ..." lines (pytest) while also
        # retaining tail summaries; this improves excerpt grounding for batch fixes.
        stdout_text = self._artifact_peek_text(failed.stdout, head_bytes=40000, tail_bytes=40000) if failed.stdout else ""
        stderr_text = self._artifact_peek_text(failed.stderr, head_bytes=40000, tail_bytes=40000) if failed.stderr else ""
        text = (stdout_text + "\n" + stderr_text).strip()

        out: list[str] = []
        included: set[str] = set()

        def add_excerpt(path: Path, *, start: int = 1, end: int = 200) -> bool:
            nonlocal out
            rel = path.as_posix().strip() or "."
            if rel in included:
                return False
            try:
                rr = self.toolbox.read_file(agent_id="router", path=rel, start_line=start, end_line=end)
            except Exception:
                return False
            included.add(rel)
            block = f"<<< {rr.pointer} >>>\n{rr.content}".strip()
            if not block:
                return False
            out.append(block)
            return True

        # Include project config (helps missing-module / script issues).
        add_excerpt(workdir / "package.json", start=1, end=200)
        add_excerpt(workdir / "tsconfig.json", start=1, end=200)

        lower_text = text.lower()
        # If DB/tooling smells show up, include the db module (often the root cause of TS errors).
        try:
            db_smell = ("pool" in lower_text) or ("knex" in lower_text) or bool(re.search(r"\bdb\s*\(", lower_text))
        except Exception:
            db_smell = False
        if db_smell:
            add_excerpt(workdir / "src" / "db.ts", start=1, end=200)
            # If the repo already contains a working example of DB usage, include it as a reference snippet.
            add_excerpt(workdir / "src" / "routes" / "posts.ts", start=1, end=220)

        # TypeScript compiler error lines: path(line,col): error TSxxxx: message
        ts_pat = re.compile(
            r"^(?P<file>[^\(\s]+)\((?P<line>\d+),(?P<col>\d+)\):\s+error\s+TS\d+:\s+(?P<msg>.*)$",
            re.MULTILINE,
        )
        files: list[tuple[str, int]] = []
        for m in ts_pat.finditer(text):
            f = (m.group("file") or "").replace("\\", "/").strip()
            try:
                line = int(m.group("line"))
            except Exception:
                line = 1
            if f:
                files.append((f, max(1, line)))

        # If types are involved, include a likely shared types file when present.
        if "on type 'user'" in text.lower() or "on type 'post'" in text.lower() or "on type 'comment'" in text.lower():
            add_excerpt(workdir / "src" / "types.ts", start=1, end=220)

        error_added = 0
        for f, line in files:
            if error_added >= max_error_files:
                break
            if add_excerpt(workdir / f, start=max(1, line - 25), end=line + 25):
                error_added += 1
            if sum(len(x) for x in out) > max_chars:
                break

        # Python/pytest: include the failing test module and import target modules when obvious.
        try:
            lower = text.lower()
            is_pytest = "pytest" in str(failed.command or "").lower()
            is_python = is_pytest or ("importerror" in lower) or ("modulenotfounderror" in lower) or ("cannot import name" in lower)
        except Exception:
            is_python = False

        if is_python:
            # ImportError while importing test module 'tests/xxx.py'
            try:
                m = re.search(r"importing test module ['\"](?P<path>tests[/\\].+?\.py)['\"]", text, flags=re.IGNORECASE)
                if m:
                    p = (m.group("path") or "").replace("\\", "/").strip()
                    if p:
                        add_excerpt(Path(p), start=1, end=220)
            except Exception:
                pass

            # ERROR tests/xxx.py lines
            try:
                for m in re.finditer(r"^ERROR\s+(?P<path>tests[/\\].+?\.py)\s*$", text, flags=re.MULTILINE):
                    p = (m.group("path") or "").replace("\\", "/").strip()
                    if p:
                        add_excerpt(Path(p), start=1, end=220)
                    if sum(len(x) for x in out) > max_chars:
                        break
            except Exception:
                pass

            # cannot import name 'X' from 'pkg.mod'
            try:
                m = re.search(
                    r"cannot import name ['\"](?P<sym>[^'\"]+)['\"] from ['\"](?P<mod>[^'\"]+)['\"]",
                    text,
                    flags=re.IGNORECASE,
                )
                if m:
                    mod = (m.group("mod") or "").strip()
                    rel = mod.replace(".", "/").strip("/")
                    for c in [f"{rel}.py", f"{rel}/__init__.py"]:
                        if (self.repo_root / c).exists():
                            add_excerpt(Path(c), start=1, end=240)
                    # also include parent package __init__.py when present
                    parts = rel.split("/")
                    if len(parts) >= 2:
                        parent = "/".join(parts[:-1])
                        p_init = f"{parent}/__init__.py"
                        if (self.repo_root / p_init).exists():
                            add_excerpt(Path(p_init), start=1, end=220)
            except Exception:
                pass

            # ModuleNotFoundError: No module named 'x.y'
            try:
                m = re.search(r"No module named ['\"](?P<mod>[^'\"]+)['\"]", text, flags=re.IGNORECASE)
                if m:
                    mod = (m.group("mod") or "").strip()
                    if mod and not mod.startswith("."):
                        rel = mod.replace(".", "/").strip("/")
                        for c in [f"{rel}.py", f"{rel}/__init__.py"]:
                            if (self.repo_root / c).exists():
                                add_excerpt(Path(c), start=1, end=240)
            except Exception:
                pass

        blob = "\n\n".join(out).strip()
        if len(blob) > max_chars:
            blob = blob[:max_chars] + "\n…（摘录过长，已截断）…"
        return blob

    def _build_test_failure_harvest(
        self, *, report: packs.TestReport, blocker_text: str
    ) -> tuple[dict[str, Any], Optional[str]]:
        """
        Harvest a broader set of error signals from the failing command output (head+tail),
        plus a few deterministic "doctor" hints. This is NOT a second test run.
        """

        failed: Optional[packs.TestResult] = None
        for r in report.results:
            if not r.passed:
                failed = r
                break
        if not failed:
            return {}, None

        failed_cmd = str(failed.command or "").strip()
        workdir = self._shell_cd_dir(failed_cmd)

        stdout_text = self._artifact_peek_text(failed.stdout, head_bytes=60000, tail_bytes=60000) if failed.stdout else ""
        stderr_text = self._artifact_peek_text(failed.stderr, head_bytes=60000, tail_bytes=60000) if failed.stderr else ""
        raw = (stdout_text + "\n" + stderr_text).strip()

        signals = self._extract_error_signals(raw, limit=60)

        ts_by_file: dict[str, list[str]] = {}
        ts_pat = re.compile(
            r"^(?P<file>[^\(\s]+)\((?P<line>\d+),(?P<col>\d+)\):\s+error\s+TS\d+:\s+(?P<msg>.*)$",
            re.MULTILINE,
        )
        for m in ts_pat.finditer(raw):
            f = (m.group("file") or "").replace("\\", "/").strip()
            if not f:
                continue
            line = m.group("line") or "1"
            col = m.group("col") or "1"
            msg = (m.group("msg") or "").strip()
            entry = f"{f}({line},{col}): {msg}".strip()
            ts_by_file.setdefault(f, [])
            if entry not in ts_by_file[f]:
                ts_by_file[f].append(entry)
            if len(ts_by_file[f]) > 6:
                ts_by_file[f] = ts_by_file[f][:6]
            if len(ts_by_file) > 12:
                break

        pytest_by_file: dict[str, list[str]] = {}
        for m in re.finditer(r"^(?:ERROR|FAILED)\s+(?P<node>.+)$", raw, flags=re.MULTILINE):
            node = (m.group("node") or "").strip()
            if not node:
                continue
            file_part = node.split("::", 1)[0].strip()
            if file_part.startswith("tests/") or file_part.startswith("tests\\"):
                f = file_part.replace("\\", "/")
                pytest_by_file.setdefault(f, [])
                if node not in pytest_by_file[f]:
                    pytest_by_file[f].append(node)
                if len(pytest_by_file[f]) > 6:
                    pytest_by_file[f] = pytest_by_file[f][:6]
            if len(pytest_by_file) > 12:
                break

        doctor_findings: list[str] = []
        doctor_ptrs: list[str] = []

        # Doctor: Node missing dependency.
        try:
            missing_pkgs: list[str] = []
            for m in re.finditer(r"Cannot find module ['\"](?P<pkg>[^'\"]+)['\"]", raw):
                pkg = (m.group("pkg") or "").strip()
                if not pkg:
                    continue
                if pkg.startswith(".") or pkg.startswith("/") or pkg.startswith("\\"):
                    continue
                if pkg in missing_pkgs:
                    continue
                missing_pkgs.append(pkg)
                if len(missing_pkgs) >= 6:
                    break

            pkg_path = self.repo_root / workdir / "package.json"
            if missing_pkgs and pkg_path.exists():
                try:
                    pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
                    deps = dict(pkg.get("dependencies") or {})
                    dev = dict(pkg.get("devDependencies") or {})
                    missing = [p for p in missing_pkgs if p not in deps and p not in dev]
                except Exception:
                    missing = missing_pkgs
                if missing:
                    doctor_findings.append(
                        f"Node 依赖缺失：{', '.join(missing[:6])} 不在 {(workdir / 'package.json').as_posix()} 的 dependencies/devDependencies 中。"
                    )
                    try:
                        rr = self.toolbox.read_file(
                            agent_id="router", path=(workdir / "package.json").as_posix(), start_line=1, end_line=140
                        )
                        if rr.pointer:
                            doctor_ptrs.append(rr.pointer)
                    except Exception:
                        pass
        except Exception:
            pass

        # Doctor: Python cannot import name -> point to the module file.
        try:
            # Example: cannot import name 'MockParser' from 'src.parsers.mock_parser'
            m = re.search(r"cannot import name ['\"](?P<sym>[^'\"]+)['\"] from ['\"](?P<mod>[^'\"]+)['\"]", raw)
            if m:
                sym = (m.group("sym") or "").strip()
                mod = (m.group("mod") or "").strip()
                if sym and mod:
                    doctor_findings.append(f"Python 导入失败：从 {mod} 导入 {sym} 失败；检查该模块是否导出该符号，或修正 import。")
                    rel = mod.replace(".", "/").strip("/")
                    candidates = [f"{rel}.py", f"{rel}/__init__.py"]
                    for c in candidates:
                        abs_path = self.repo_root / c
                        if not abs_path.exists():
                            continue
                        try:
                            rr = self.toolbox.read_file(agent_id="router", path=c, start_line=1, end_line=220)
                            if rr.pointer:
                                doctor_ptrs.append(rr.pointer)
                        except Exception:
                            pass
                        break
        except Exception:
            pass

        total_ts = sum(len(v) for v in ts_by_file.values())
        total_py = sum(len(v) for v in pytest_by_file.values())
        summary = ""
        if total_ts:
            summary = f"TypeScript 编译错误：{total_ts} 条（{len(ts_by_file)} 个文件）"
        elif total_py:
            summary = f"Pytest 错误：{total_py} 条（{len(pytest_by_file)} 个文件）"
        elif signals:
            summary = signals[0][:160]
        else:
            summary = "测试/构建失败（未能提取更多错误信号）"

        pointers: list[str] = []
        for p in [failed.stdout, failed.stderr, failed.meta]:
            s = str(p or "").strip()
            if s and s not in pointers:
                pointers.append(s)
        for p in doctor_ptrs:
            if p and p not in pointers:
                pointers.append(p)

        harvest: dict[str, Any] = {
            "summary": summary,
            "failed_command": failed_cmd,
            "workdir": workdir.as_posix(),
            "signals": signals[:60],
            "ts_by_file": ts_by_file,
            "pytest_by_file": pytest_by_file,
            "doctor_findings": doctor_findings[:10],
            "pointers": pointers[:24],
        }
        ptr = self.artifacts.put_json(harvest, suffix=".harvest.json", kind="harvest").to_pointer()
        return harvest, ptr

    def _format_harvest_for_prompt(self, *, harvest: dict[str, Any], pointer: Optional[str]) -> str:
        if not pointer or not harvest:
            return ""

        lines: list[str] = []
        lines.append("FailureHarvest（扩大采样：从失败命令输出中一次提取更多错误信号，便于批量修复；事实以 pointers 展开为准）：")
        lines.append(f"FailureHarvestPointer: {pointer}")
        summary = str(harvest.get("summary") or "").strip()
        if summary:
            lines.append(f"Summary: {summary[:200]}")
        cmd = str(harvest.get("failed_command") or "").strip()
        if cmd:
            lines.append(f"FailedCommand: {cmd[:240]}")

        findings = [str(x).strip() for x in (harvest.get("doctor_findings") or []) if str(x).strip()]
        if findings:
            lines.append("DoctorFindings:")
            for f in findings[:6]:
                lines.append(f"- {f[:220]}")

        by_file = harvest.get("ts_by_file") or {}
        if isinstance(by_file, dict) and by_file:
            lines.append("TS Errors (by file, top):")
            shown = 0
            for k, v in list(by_file.items())[:4]:
                lines.append(f"- {str(k)[:180]}")
                for it in list(v or [])[:3]:
                    lines.append(f"  - {str(it)[:220]}")
                shown += 1
                if shown >= 4:
                    break

        py_by_file = harvest.get("pytest_by_file") or {}
        if isinstance(py_by_file, dict) and py_by_file:
            lines.append("Pytest (by file, top):")
            shown = 0
            for k, v in list(py_by_file.items())[:4]:
                lines.append(f"- {str(k)[:180]}")
                for it in list(v or [])[:3]:
                    lines.append(f"  - {str(it)[:220]}")
                shown += 1
                if shown >= 4:
                    break

        return "\n".join(lines).strip()

    def _fix_loop_autohint_for_tests(self, *, report: packs.TestReport, blocker_text: str) -> str:
        """
        Provide a small deterministic hint for common failure patterns to help coders converge.
        This must stay short and evidence-based (point to repo snippets).
        """
        try:
            failed: Optional[packs.TestResult] = None
            for r in report.results:
                if not r.passed:
                    failed = r
                    break
            if not failed:
                return ""
            cmd_dir = self._shell_cd_dir(failed.command or "")
            if cmd_dir == Path("."):
                return ""
        except Exception:
            return ""

        text = (blocker_text or "").strip()
        lower = text.lower()

        # 0) Windows: Node spawn UNKNOWN is frequently caused by spawning `.bin/*.exe` directly.
        if os.name == "nt" and ("spawn unknown" in lower or "syscall: 'spawn'" in lower):
            lines: list[str] = []
            lines.append("检测到 `spawn UNKNOWN`（Windows 常见环境/脚本问题）：")
            lines.append("- 常见根因：脚本直接执行 `node_modules/.bin/<tool>.exe`，可能选到 0 字节占位 shim；或应执行 `<tool>.cmd`。")
            lines.append("- 修复优先级：先让失败命令可稳定通过（build/lint/test），再改业务逻辑。")
            lines.append("- 建议：通过 `npm run <script>` 调用工具（让 PATH shim 生效）；或在 Windows 上优先 `.cmd` 并跳过 size==0 的 `.exe`。")
            return "\n".join(lines).strip()

        if os.name == "nt" and ("0-byte" in lower or "0 byte" in lower) and "node_modules" in lower and ".bin" in lower:
            return (
                "检测到 Windows 下 `node_modules/.bin/<tool>.exe` 是 0 字节占位 shim（常见于 npm 工具链）。\n"
                "- 不要把该 `.exe` 当成真实可执行文件；优先通过 `npm run <script>`/`npx <tool>` 调用。\n"
                "- 如果脚本硬编码了 `<tool>.exe`：改用 `<tool>.cmd`，或改为调用 npm script（让 shim 自动选择正确入口）。"
            )

        # 1) Missing external CLI (non-npm). Pivot the implementation to avoid requiring a global binary.
        try:
            m = re.search(
                r"'(?P<bin>[^']+)'\s+is\s+not\s+recognized\s+as\s+an\s+internal\s+or\s+external\s+command",
                text,
                flags=re.IGNORECASE,
            )
            missing_bin = (m.group("bin") or "").strip() if m else ""
            if not missing_bin:
                m = re.search(r"\bcommand\s+not\s+found:\s+(?P<bin>[A-Za-z0-9._-]+)\b", text, flags=re.IGNORECASE)
                missing_bin = (m.group("bin") or "").strip() if m else ""
            if not missing_bin:
                m = re.search(r"\bspawn\s+(?P<bin>[A-Za-z0-9._-]+)\s+enoent\b", text, flags=re.IGNORECASE)
                missing_bin = (m.group("bin") or "").strip() if m else ""
        except Exception:
            missing_bin = ""

        known_local_bins = {"tsc", "typescript", "eslint", "jest", "vitest", "prettier"}
        if missing_bin and missing_bin.upper() != "NODE_ENV" and missing_bin.lower() not in known_local_bins:
            b = missing_bin.strip()
            if b and len(b) <= 40:
                return (
                    f"检测到失败命令依赖缺失的外部 CLI：`{b}`（当前环境 PATH 未提供该命令）。\n"
                    "- 不要把“让用户手动装全局 CLI”当作前置条件；优先把项目改成不依赖该 CLI 的方案。\n"
                    "- 如果是网站/前端：优先使用 npm 工程骨架（Vite/React/Express 等），依赖写入 package.json。\n"
                    "- 如果是静态站点：优先用纯 HTML/CSS/JS 或 Node 工具链实现 MVP，再迭代。\n"
                    "- 同步更新 README：写清楚 `npm install` / `npm run dev` / `npm test` 的可复现步骤。"
                )

        # Only attempt for TS/Node-ish errors.
        if not any(k in lower for k in ["tsc", "error ts", "typescript", "ts2349", "call signatures", "pool"]):
            return ""

        # Evidence snippets.
        db_ptr = ""
        db_text = ""
        posts_ptr = ""
        posts_text = ""
        try:
            db_rel = (cmd_dir / "src" / "db.ts").as_posix()
            if (self.repo_root / db_rel).exists():
                rr = self.toolbox.read_file(agent_id="router", path=db_rel, start_line=1, end_line=80)
                db_ptr = rr.pointer
                db_text = rr.content
        except Exception:
            db_ptr = ""
            db_text = ""

        try:
            posts_rel = (cmd_dir / "src" / "routes" / "posts.ts").as_posix()
            if (self.repo_root / posts_rel).exists():
                rr = self.toolbox.read_file(agent_id="router", path=posts_rel, start_line=1, end_line=140)
                posts_ptr = rr.pointer
                posts_text = rr.content
        except Exception:
            posts_ptr = ""
            posts_text = ""

        db_lower = db_text.lower()
        is_pool = ("from 'pg'" in db_lower or 'from "pg"' in db_lower) and ("new pool" in db_lower)
        is_knex = ("from 'knex'" in db_lower or 'from "knex"' in db_lower) or ("knex(" in db_lower)

        # If db exports pg.Pool but code calls db('table') / db.insert, point it out with options.
        pool_type_error = any(k in lower for k in ["type 'pool' has no call signatures", "property 'insert' does not exist on type 'pool'", "property 'select' does not exist on type 'pool'"])
        db_call_smell = bool(re.search(r"\bdb\s*\(", lower)) or ("db(" in db_lower and "export default" in db_lower)

        if is_pool and (pool_type_error or "call signatures" in lower):
            lines: list[str] = []
            lines.append("检测到数据库层不一致，可能导致 TypeScript 编译错误反复出现：")
            if db_ptr:
                lines.append(f"- 证据：`db` 来自 `{db_ptr}`（看起来是 `pg.Pool`）。")
            if posts_ptr and "pool.query" in (posts_text.lower()):
                lines.append(f"- 证据：同仓库里已有 `pool.query` 用法：`{posts_ptr}`。")
            lines.append("修复方向（二选一，选一个并把 `npm run build` 跑通）：")
            lines.append("1) 保持 `pg.Pool`：把 controllers 里 `db('table')...` 改为 `pool.query(...)`（参照 posts.ts）。")
            lines.append("2) 统一用 Knex：把 `src/db.ts` 改为导出 `knex(...)` 实例，并把 `pool.query` 相关代码改为 Knex 写法。")
            return "\n".join(lines).strip()

        if is_knex and "pool.query" in lower:
            # Rare but symmetrical case.
            if db_ptr:
                return (
                    "检测到 `src/db.ts` 可能是 Knex，但当前失败日志里出现了 `pool.query`。"
                    f"请以 `{db_ptr}` 为准统一 DB 用法，让 `npm run build` 通过。"
                )

        kb = best_knowledge_snippet(text, max_lines=8, repo_root=self.repo_root)
        return kb or ""

    def _incident_for_tests(
        self,
        *,
        report: packs.TestReport,
        blocker_text: str,
        activated_agents: Set[str],
    ) -> packs.IncidentPack:
        """
        Build a deterministic "incident capsule" for test/build/lint failures.

        Goal: turn noisy error output into a small, auditable, pointer-backed brief so the
        next agent can act without guessing.
        """

        text = (blocker_text or "").strip()
        lower = text.lower()

        failed_cmd = ""
        cmd_dir = Path(".")
        try:
            for r in report.results:
                if not r.passed:
                    failed_cmd = r.command or ""
                    cmd_dir = self._shell_cd_dir(failed_cmd)
                    break
        except Exception:
            failed_cmd = ""
            cmd_dir = Path(".")

        suggested_fix_agent = self._select_fix_coder_for_tests(report=report, blocker_text=text, activated_agents=activated_agents)

        evidence: list[str] = []
        for p in list(report.pointers or [])[:24]:
            s = str(p).strip()
            if s and s not in evidence:
                evidence.append(s)

        # Add quick pointers to the local node workspace config when present.
        try:
            for rel in [
                (cmd_dir / "package.json").as_posix(),
                (cmd_dir / "tsconfig.json").as_posix(),
                (cmd_dir / ".eslintrc.json").as_posix(),
                (cmd_dir / ".eslintrc.js").as_posix(),
                (cmd_dir / "eslint.config.js").as_posix(),
                (cmd_dir / "eslint.config.mjs").as_posix(),
            ]:
                if rel == ".":
                    continue
                if not (self.repo_root / rel).exists():
                    continue
                rr = self.toolbox.read_file(agent_id="router", path=rel, start_line=1, end_line=220)
                if rr.pointer and rr.pointer not in evidence:
                    evidence.append(rr.pointer)
        except Exception:
            pass

        category = "tests_failed"
        summary = "测试/构建失败"
        diagnosis: list[str] = []
        next_steps: list[str] = []
        required_caps: list[str] = []

        autohint = self._fix_loop_autohint_for_tests(report=report, blocker_text=text) or None

        # Windows + eslint glob quoting pitfall: single quotes become literal in cmd.exe / npm.
        if ("eslint" in lower) and ("no files matching the pattern" in lower or "no matching files" in lower):
            # Look for telltale "'...'" fragments in the error output.
            if "\"'" in text or "pattern \"'" in lower or "pattern \"'\"" in lower or "pattern \"'server/" in lower:
                category = "eslint_glob_quoting_windows"
                summary = "ESLint glob 引号导致找不到文件（Windows）"
                diagnosis.append("Windows 的 npm scripts 不会像 bash 那样处理单引号；单引号会作为字面量传给 eslint。")
                diagnosis.append("因此 `eslint 'server/**/*.ts'` 可能匹配不到任何文件，表现为 `No files matching the pattern \"'...\"`。")
                next_steps.append("把 npm scripts 里的 glob 改为双引号：`eslint \"server/**/*.ts\" ...`，避免单引号。")
                next_steps.append("重新运行失败命令（lint/build/test）验证。")
                required_caps.extend(["node", "eslint", "windows"])
            else:
                category = "eslint_no_matching_files"
                summary = "ESLint 找不到匹配文件"
                diagnosis.append("ESLint 报告没有匹配到文件；可能是脚本 glob 写错，或目录/扩展名与实际不一致。")
                next_steps.append("对照 `package.json` 的 eslint 脚本与仓库目录结构，确认匹配模式与实际文件位置一致。")
                required_caps.extend(["node", "eslint"])

        elif "eslint" in lower:
            category = "eslint_failed"
            summary = "ESLint 失败"
            diagnosis.append("lint 命令执行失败；通常是配置缺失/规则报错/依赖缺失。")
            next_steps.append("优先查看失败命令输出中的第一条错误，确保 eslint 配置与依赖完整。")
            required_caps.extend(["node", "eslint"])

        elif any(k in lower for k in ["tsc", "error ts", "typescript", ".ts(", ".tsx("]):
            category = "typescript_compile_failed"
            summary = "TypeScript 编译失败"
            diagnosis.append("TypeScript 编译未通过；常见原因：类型不一致、缺少模块、tsconfig 路径/别名配置错误。")
            next_steps.append("以 `tsc` 报错的首个文件/行号为准，修复类型或 import；如引入新依赖需同步更新 package.json。")
            required_caps.extend(["node", "typescript"])

        elif "cannot find module" in lower or "module not found" in lower:
            category = "missing_module_or_dep"
            summary = "缺少依赖或 import 路径错误"
            diagnosis.append("构建/测试失败日志提示缺少模块；要么依赖未安装/未声明，要么 import 路径指向不存在文件。")
            next_steps.append("若是第三方包：补齐 dependencies/devDependencies，并确保 `npm install` 后可解析。")
            next_steps.append("若是本地文件：修正相对路径或在代码变更中创建对应文件。")
            required_caps.extend(["node", "deps"])

        blocker_short = text
        if len(blocker_short) > 1800:
            blocker_short = blocker_short[:1800] + "…（已截断）…"

        # De-duplicate capability tags.
        caps: list[str] = []
        seen: set[str] = set()
        for c in required_caps:
            s = str(c).strip().lower()
            if not s or s in seen:
                continue
            seen.add(s)
            caps.append(s)

        return packs.IncidentPack(
            source="tests",
            category=category,
            summary=summary,
            blocker=blocker_short,
            evidence_pointers=evidence[:32],
            diagnosis=diagnosis[:8],
            next_steps=next_steps[:8],
            required_capabilities=caps[:12],
            suggested_fix_agent=suggested_fix_agent,
            autohint=autohint,
        )

    def _auto_code_change_for_test_failure(
        self, *, report: packs.TestReport, blocker_text: str
    ) -> Optional[packs.CodeChange]:
        """
        Deterministic, low-risk auto-fixes for common scaffold failures.

        This is intentionally conservative: only create missing *configuration/entry* files
        when the error output clearly indicates they are absent.
        """
        text = (blocker_text or "").strip()
        lower = text.lower()

        def repo_has_any(paths: list[str]) -> bool:
            for p in paths:
                if (self.repo_root / p).exists():
                    return True
            return False

        failed_cmd = ""
        try:
            for r in report.results:
                if not r.passed:
                    failed_cmd = str(r.command or "")
                    break
        except Exception:
            failed_cmd = ""

        failed_node_dir = Path(".")
        if failed_cmd:
            try:
                failed_node_dir = self._shell_cd_dir(failed_cmd)
            except Exception:
                failed_node_dir = Path(".")

        def first_error_file_path() -> Optional[Path]:
            # Prefer absolute Windows paths from Node/Python stack traces.
            m = re.search(
                r"(?P<file>[A-Za-z]:[\\/][^:\r\n]+?\.(?:js|cjs|mjs|ts|tsx)):(?P<line>\d+)(?::\d+)?",
                text,
            )
            if not m:
                return None
            raw = (m.group("file") or "").strip().strip('"').strip("'")
            if not raw:
                return None
            p = Path(raw)
            try:
                if p.is_absolute() and self.repo_root in p.parents:
                    return p
            except Exception:
                pass
            # Best-effort: treat it as relative to repo_root.
            try:
                rel_guess = raw.replace("\\", "/").lstrip("/")
                p2 = (self.repo_root / rel_guess).resolve()
                if p2.exists() and p2.is_file():
                    return p2
            except Exception:
                pass
            return None

        def parse_script_name(cmd: str) -> str:
            c = cmd or ""
            # Examples:
            # - cd /d "client" && npm run lint
            # - cd /d "client" && npm test
            # - pnpm run build
            m = re.search(r"\b(?:npm|pnpm|yarn)\s+run\s+(?P<name>[A-Za-z0-9:_-]+)\b", c, flags=re.IGNORECASE)
            if m:
                return (m.group("name") or "").strip()
            m = re.search(r"\b(?:npm|pnpm|yarn)\s+(?P<name>test|lint|build)\b", c, flags=re.IGNORECASE)
            if m:
                return (m.group("name") or "").strip()
            return ""

        # 0) Node/CJS: models sometimes "polyfill" __dirname/__filename incorrectly and redeclare them.
        # In CommonJS, these are already defined by Node; redeclaration causes a SyntaxError.
        if "identifier '__dirname' has already been declared" in lower or "identifier '__filename' has already been declared" in lower:
            try:
                p = first_error_file_path()
                if p and p.exists():
                    src = p.read_text(encoding="utf-8", errors="replace")
                    lines = src.splitlines(True)
                    out_lines: list[str] = []
                    changed = False
                    for ln in lines:
                        s = ln.strip()
                        if re.match(r"^(?:const|let|var)\s+__dirname\s*=", s):
                            changed = True
                            continue
                        if re.match(r"^(?:const|let|var)\s+__filename\s*=", s):
                            changed = True
                            continue
                        out_lines.append(ln)
                    if changed:
                        rel = p.relative_to(self.repo_root).as_posix()
                        return packs.CodeChange(
                            kind="patch",
                            summary=f"auto-fix: remove duplicate __dirname/__filename declarations in {rel}",
                            writes=[packs.FileWrite(path=rel, content="".join(out_lines))],
                            files_changed=[rel],
                            blockers=[],
                        )
            except Exception:
                pass

        # 0a) Hugo (Windows): `hugo-bin` can install the real binary under `node_modules/hugo-bin/vendor/`
        # while `node_modules/.bin/hugo.exe` is missing or 0-byte (shim). As a last-resort workaround for
        # scripts that hardcode `.bin/hugo.exe`, copy the vendor binary into `.bin`.
        if os.name == "nt" and ("hugo" in lower) and ("0-byte" in lower or "0 byte" in lower or "no valid hugo binary found" in lower):
            try:
                base = (self.repo_root / failed_node_dir).resolve()
                src_abs = (base / "node_modules" / "hugo-bin" / "vendor" / "hugo.exe").resolve()
                dst_abs = (base / "node_modules" / ".bin" / "hugo.exe").resolve()
                if src_abs.exists() and src_abs.is_file():
                    try:
                        src_size = int(src_abs.stat().st_size)
                    except Exception:
                        src_size = 0
                    try:
                        dst_size = int(dst_abs.stat().st_size) if dst_abs.exists() else 0
                    except Exception:
                        dst_size = 0
                    if src_size > 0 and dst_size == 0:
                        rel_src = src_abs.relative_to(self.repo_root).as_posix()
                        rel_dst = dst_abs.relative_to(self.repo_root).as_posix()
                        return packs.CodeChange(
                            kind="patch",
                            summary="auto-fix: copy hugo-bin vendor binary into node_modules/.bin (Windows workaround)",
                            copies=[packs.FileCopy(src=rel_src, dst=rel_dst)],
                            files_changed=[rel_dst],
                            blockers=[],
                        )
            except Exception:
                pass

        def extract_missing_bin(err_text: str) -> str:
            t = err_text or ""
            # Windows cmd.exe style:  'tsc' is not recognized as an internal or external command
            m = re.search(
                r"'(?P<bin>[^']+)'\s+is\s+not\s+recognized\s+as\s+an\s+internal\s+or\s+external\s+command",
                t,
                flags=re.IGNORECASE,
            )
            if m:
                return (m.group("bin") or "").strip()
            # POSIX: sh: tsc: not found / command not found: tsc
            m = re.search(r"\bsh:\s+(?P<bin>[A-Za-z0-9._-]+):\s+not\s+found\b", t, flags=re.IGNORECASE)
            if m:
                return (m.group("bin") or "").strip()
            m = re.search(r"\bcommand\s+not\s+found:\s+(?P<bin>[A-Za-z0-9._-]+)\b", t, flags=re.IGNORECASE)
            if m:
                return (m.group("bin") or "").strip()
            # Node spawn ENOENT
            m = re.search(r"\bspawn\s+(?P<bin>[A-Za-z0-9._-]+)\s+enoent\b", t, flags=re.IGNORECASE)
            if m:
                return (m.group("bin") or "").strip()
            return ""

        # 0) Windows/Node: npm tools typically expose `.cmd`/`.ps1` wrappers under `node_modules/.bin`.
        # A `.exe` may be missing or be a zero-byte placeholder shim; prefer `.cmd` for scripts that hardcode `.exe`.
        if os.name == "nt" and re.search(r"node_modules[\\/](?:\.bin|bin)[\\/]", lower or ""):
            try:
                m = re.search(r"node_modules[\\/]\.bin[\\/](?P<tool>[A-Za-z0-9_.-]+)\.exe", text, flags=re.IGNORECASE)
                if not m:
                    # Common typo: `node_modules/bin/<tool>.exe` (missing dot)
                    m = re.search(r"node_modules[\\/]bin[\\/](?P<tool>[A-Za-z0-9_.-]+)\.exe", text, flags=re.IGNORECASE)
                tool = (m.group("tool") or "").strip() if m else ""
                if tool and len(tool) <= 60:
                    bin_dir = (self.repo_root / failed_node_dir / "node_modules" / ".bin").resolve()
                    exe_path = (bin_dir / f"{tool}.exe").resolve()
                    noext_path = (bin_dir / f"{tool}").resolve()
                    cmd_path = (bin_dir / f"{tool}.cmd").resolve()
                    if cmd_path.exists():
                        try:
                            exe_size = int(exe_path.stat().st_size) if exe_path.exists() else None
                        except Exception:
                            exe_size = None
                        try:
                            noext_size = int(noext_path.stat().st_size) if noext_path.exists() else None
                        except Exception:
                            noext_size = None

                        # Only auto-fix when the `.exe` is clearly bogus/missing. On Windows,
                        # npm typically provides `<tool>.cmd` as the real entrypoint; a `.exe`
                        # may be missing or a zero-byte placeholder.
                        prefer_cmd = (exe_size is None) or (exe_size == 0) or (noext_size == 0)
                        if prefer_cmd:
                            roots: list[Path] = []
                            seen: set[Path] = set()
                            for r in [
                                self.repo_root / failed_node_dir / "scripts",
                                self.repo_root / "scripts",
                            ]:
                                rr = r.resolve()
                                if rr.exists() and rr.is_dir() and rr not in seen:
                                    seen.add(rr)
                                    roots.append(rr)

                            writes: list[packs.FileWrite] = []
                            files_changed: list[str] = []
                            pat = re.compile(rf"(?i)\b{re.escape(tool)}\.exe\b")
                            for root in roots[:3]:
                                for p in list(root.rglob("*"))[:300]:
                                    if not p.is_file():
                                        continue
                                    if p.suffix.lower() not in {".js", ".cjs", ".mjs", ".ts", ".json"}:
                                        continue
                                    try:
                                        src = p.read_text(encoding="utf-8", errors="replace")
                                    except Exception:
                                        continue
                                    if not pat.search(src):
                                        continue

                                    # Avoid clobbering "manual bin/" fallbacks: only rewrite lines (or adjacent
                                    # lines) that refer to node_modules + (.bin or the common 'bin' typo).
                                    out_lines: list[str] = []
                                    changed = False
                                    in_bin_stmt = False
                                    for ln in src.splitlines(True):
                                        ll = ln.lower()
                                        node_bin_hint = ("node_modules" in ll) and (
                                            ".bin" in ll or "'bin'" in ll or '"bin"' in ll or "\\bin\\" in ll or "/bin/" in ll
                                        )
                                        if in_bin_stmt and re.match(r"^\\s*(?:const|let|var)\\b", ln) and (".bin" not in ll) and (not node_bin_hint):
                                            in_bin_stmt = False
                                        if not in_bin_stmt and (".bin" in ll or node_bin_hint):
                                            in_bin_stmt = True
                                        if in_bin_stmt and pat.search(ln):
                                            nl = pat.sub(f"{tool}.cmd", ln)
                                            # Also correct the common `node_modules/bin` typo -> `node_modules/.bin`.
                                            if "node_modules" in ll and ".bin" not in ll:
                                                nl = re.sub(r"(?i)(['\"])bin\1", r"\1.bin\1", nl)
                                            changed = changed or (nl != ln)
                                            out_lines.append(nl)
                                        else:
                                            out_lines.append(ln)
                                        if in_bin_stmt and ";" in ln:
                                            in_bin_stmt = False
                                    if not changed:
                                        continue
                                    dst = "".join(out_lines)

                                    rel = p.relative_to(self.repo_root).as_posix()
                                    writes.append(packs.FileWrite(path=rel, content=dst))
                                    files_changed.append(rel)
                                    if len(writes) >= 6:
                                        break
                                if len(writes) >= 6:
                                    break

                            if writes:
                                return packs.CodeChange(
                                    kind="patch",
                                    summary=f"auto-fix: prefer `{tool}.cmd` over `{tool}.exe` in node_modules/.bin on Windows",
                                    writes=writes,
                                    files_changed=files_changed,
                                    blockers=[],
                                )
            except Exception:
                pass

        # 0) Windows/Node: `spawn UNKNOWN` often comes from spawning `.bin/*.exe` shims directly.
        # If we can see a `.cmd` wrapper for the same tool, prefer `.cmd` (and avoid 0-byte `.exe` placeholders).
        if os.name == "nt":
            spawn_unknown = re.search(
                r"\bspawn\s+(?P<path>[^\r\n]+?)\s+UNKNOWN\b", text, flags=re.IGNORECASE
            )
            if spawn_unknown or "syscall: 'spawn'" in lower:
                try:
                    m = spawn_unknown or re.search(
                        r"\bspawn\s+(?P<path>[^\r\n]+?)\s+UNKNOWN\b", text, flags=re.IGNORECASE
                    )
                    spath = (m.group("path") if m else "") or ""
                    spath = spath.strip().strip('"').strip("'").strip()
                    tool = Path(spath).stem if spath else ""
                    if tool and len(tool) <= 60:
                        candidates: list[tuple[Path, Path]] = []
                        for base in [failed_node_dir, Path(".")]:
                            candidates.append(
                                (
                                    (self.repo_root / base / "node_modules" / ".bin" / f"{tool}.exe").resolve(),
                                    (self.repo_root / base / "node_modules" / ".bin" / f"{tool}.cmd").resolve(),
                                )
                            )
                        exe_path: Optional[Path] = None
                        cmd_path: Optional[Path] = None
                        for ex, cm in candidates:
                            if ex.exists() and cm.exists():
                                exe_path, cmd_path = ex, cm
                                break
                        if exe_path and cmd_path:
                            try:
                                exe_size = int(exe_path.stat().st_size)
                            except Exception:
                                exe_size = -1
                            # Only auto-fix when the `.exe` is clearly bogus (0 bytes), to avoid breaking legit native bins.
                            if exe_size == 0:
                                roots: list[Path] = []
                                for r in [
                                    self.repo_root / failed_node_dir / "scripts",
                                    self.repo_root / "scripts",
                                ]:
                                    if r.exists() and r.is_dir():
                                        roots.append(r)

                                writes: list[packs.FileWrite] = []
                                files_changed: list[str] = []
                                pat = re.compile(rf"(?i)\b{re.escape(tool)}\.exe\b")
                                for root in roots[:3]:
                                    for p in list(root.rglob("*"))[:200]:
                                        if not p.is_file():
                                            continue
                                        if p.suffix.lower() not in {".js", ".cjs", ".mjs", ".ts"}:
                                            continue
                                        try:
                                            src = p.read_text(encoding="utf-8", errors="replace")
                                        except Exception:
                                            continue
                                        if not pat.search(src):
                                            continue
                                        dst = pat.sub(f"{tool}.cmd", src)
                                        if dst == src:
                                            continue
                                        rel = p.relative_to(self.repo_root).as_posix()
                                        writes.append(packs.FileWrite(path=rel, content=dst))
                                        files_changed.append(rel)
                                        if len(writes) >= 6:
                                            break
                                    if len(writes) >= 6:
                                        break

                                if writes:
                                    return packs.CodeChange(
                                        kind="patch",
                                        summary=f"auto-fix: avoid 0-byte `{tool}.exe` shim by using `{tool}.cmd` on Windows",
                                        writes=writes,
                                        files_changed=files_changed,
                                        blockers=[],
                                    )
                except Exception:
                    pass

        # 1) ESLint: missing config file
        if "eslint couldn't find a configuration file" in lower or "eslint could not find a configuration file" in lower:
            # If any config already exists, don't auto-create.
            if repo_has_any(
                [
                    ".eslintrc",
                    ".eslintrc.js",
                    ".eslintrc.cjs",
                    ".eslintrc.json",
                    ".eslintrc.yaml",
                    ".eslintrc.yml",
                    "eslint.config.js",
                    "eslint.config.cjs",
                    "eslint.config.mjs",
                ]
            ):
                return None

            pkg_path = self.repo_root / "package.json"
            if not pkg_path.exists():
                return None
            try:
                pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                return None

            # If eslintConfig exists in package.json, the missing-config error shouldn't happen.
            if isinstance(pkg.get("eslintConfig"), dict):
                return None

            scripts = dict(pkg.get("scripts") or {})
            lint_script = str(scripts.get("lint") or "")
            if "eslint" not in lint_script:
                return None

            dev = dict(pkg.get("devDependencies") or {})
            # These are required to parse TS/TSX in common setups.
            if "@typescript-eslint/parser" not in dev:
                dev["@typescript-eslint/parser"] = "^6.21.0"
            if "@typescript-eslint/eslint-plugin" not in dev:
                dev["@typescript-eslint/eslint-plugin"] = "^6.21.0"
            pkg["devDependencies"] = dev

            eslintrc = """module.exports = {
  root: true,
  env: {
    es2020: true,
    node: true,
    browser: true,
  },
  parser: '@typescript-eslint/parser',
  parserOptions: {
    ecmaVersion: 2022,
    sourceType: 'module',
    ecmaFeatures: { jsx: true },
  },
  plugins: ['@typescript-eslint'],
  extends: ['eslint:recommended'],
  ignorePatterns: ['**/dist/**', '**/node_modules/**', '**/coverage/**'],
  rules: {
    'no-unused-vars': 'off',
    '@typescript-eslint/no-unused-vars': 'off',
    'no-undef': 'off',
  },
};
"""

            return packs.CodeChange(
                kind="patch",
                summary="auto-fix: add ESLint config",
                writes=[
                    packs.FileWrite(path=".eslintrc.cjs", content=eslintrc),
                    packs.FileWrite(path="package.json", content=json.dumps(pkg, ensure_ascii=False, indent=2) + "\n"),
                ],
                files_changed=[".eslintrc.cjs", "package.json"],
                blockers=[],
            )

        # 1b) ESLint: npm scripts use single-quoted globs (breaks on Windows cmd.exe)
        # Example error:
        #   No files matching the pattern "'server/**/*.ts'" were found.
        if "eslint" in lower and "no files matching the pattern" in lower:
            m = re.search(
                r'No files matching the pattern\s+"(?P<pat>.+?)"\s+were found\.',
                text,
                flags=re.IGNORECASE,
            )
            raw_pat = (m.group("pat") if m else "").strip()
            # We only auto-fix the common Windows-incompatible case: extra single quotes
            # become part of the glob string (e.g. "'server/**/*.ts'").
            if raw_pat.startswith("'") and raw_pat.endswith("'") and len(raw_pat) >= 3:
                core = raw_pat[1:-1].strip()
            else:
                core = ""

            if core and any(ch in core for ch in ("*", "?", "{", "}", "[")):
                replacement = f"\"{core}\""
                writes: list[packs.FileWrite] = []
                files_changed: list[str] = []
                for node_dir in self._find_node_project_dirs():
                    pkg_path = self.repo_root / node_dir / "package.json"
                    if not pkg_path.exists():
                        continue
                    try:
                        pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
                    except Exception:
                        continue

                    scripts = pkg.get("scripts")
                    if not isinstance(scripts, dict):
                        continue

                    new_scripts: dict[str, Any] = dict(scripts)
                    updated = False
                    for k, v in list(new_scripts.items()):
                        if not isinstance(v, str):
                            continue
                        if raw_pat not in v:
                            continue
                        new_scripts[k] = v.replace(raw_pat, replacement)
                        updated = True

                    if not updated:
                        continue

                    pkg["scripts"] = new_scripts
                    rel = (
                        (node_dir / "package.json").as_posix()
                        if str(node_dir) not in {"", "."}
                        else "package.json"
                    )
                    writes.append(packs.FileWrite(path=rel, content=json.dumps(pkg, ensure_ascii=False, indent=2) + "\n"))
                    files_changed.append(rel)

                if writes:
                    return packs.CodeChange(
                        kind="patch",
                        summary="auto-fix: normalize ESLint glob quoting in npm scripts",
                        writes=writes,
                        files_changed=files_changed,
                        blockers=[],
                    )

        # 1c) Node: common missing tool binaries (TypeScript/Jest/ESLint/Prettier) in npm scripts.
        # This happens when package.json contains e.g. "build": "tsc" but devDependencies lacks "typescript".
        missing_bin = extract_missing_bin(text)
        bin_to_pkg: dict[str, tuple[str, str]] = {
            "tsc": ("typescript", "^5.6.0"),
            "typescript": ("typescript", "^5.6.0"),
            "eslint": ("eslint", "^8.57.0"),
            "jest": ("jest", "^29.7.0"),
            "vitest": ("vitest", "^1.6.0"),
            "prettier": ("prettier", "^3.3.0"),
        }
        if missing_bin and missing_bin.lower() in {k.lower() for k in bin_to_pkg.keys()}:
            pkg_name, version = bin_to_pkg.get(missing_bin.lower(), bin_to_pkg.get(missing_bin, ("", "")))
            if pkg_name:
                pkg_path = self.repo_root / failed_node_dir / "package.json"
                if pkg_path.exists():
                    try:
                        pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
                    except Exception:
                        pkg = None
                    if isinstance(pkg, dict):
                        dev = dict(pkg.get("devDependencies") or {})
                        deps = dict(pkg.get("dependencies") or {})
                        if (pkg_name not in dev) and (pkg_name not in deps):
                            dev[pkg_name] = version
                            pkg["devDependencies"] = dev
                            rel = (
                                (failed_node_dir / "package.json").as_posix()
                                if str(failed_node_dir) not in {"", "."}
                                else "package.json"
                            )
                            return packs.CodeChange(
                                kind="patch",
                                summary=f"auto-fix: add missing devDependency `{pkg_name}`",
                                writes=[packs.FileWrite(path=rel, content=json.dumps(pkg, ensure_ascii=False, indent=2) + '\n')],
                                files_changed=[rel],
                                blockers=[],
                            )

        # 1c-ext) Missing external CLI (non-npm). Example: `hugo` not installed.
        # In this case, do NOT keep iterating in fix-loop trying random installs;
        # pivot the implementation to avoid requiring a global binary.
        if missing_bin and missing_bin.upper() != "NODE_ENV" and missing_bin.lower() not in {k.lower() for k in bin_to_pkg.keys()}:
            b = missing_bin.strip()
            if b and len(b) <= 40:
                # No deterministic patch here; provide guidance via Incident/AutoHint instead.
                return None

        # 1d) Node/Windows: env var prefix in npm scripts (e.g. NODE_ENV=production) breaks on cmd.exe.
        # Fix by adding cross-env and rewriting the failing script.
        if missing_bin and missing_bin.upper() == "NODE_ENV":
            script_name = parse_script_name(failed_cmd)
            pkg_path = self.repo_root / failed_node_dir / "package.json"
            if pkg_path.exists():
                try:
                    pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    pkg = None
                if isinstance(pkg, dict):
                    scripts = pkg.get("scripts")
                    if isinstance(scripts, dict) and scripts:
                        targets: list[str] = [script_name] if script_name and script_name in scripts else list(scripts.keys())
                        new_scripts = dict(scripts)
                        updated = False
                        for k in targets:
                            v = scripts.get(k)
                            if not isinstance(v, str):
                                continue
                            if "NODE_ENV=" not in v:
                                continue
                            if "cross-env" in v:
                                continue
                            new_scripts[k] = "cross-env " + v
                            updated = True
                            # Only patch one script if we could identify it from the failing command.
                            if script_name:
                                break
                        if updated:
                            pkg["scripts"] = new_scripts
                            dev = dict(pkg.get("devDependencies") or {})
                            deps = dict(pkg.get("dependencies") or {})
                            if ("cross-env" not in dev) and ("cross-env" not in deps):
                                dev["cross-env"] = "^7.0.3"
                                pkg["devDependencies"] = dev
                            rel = (
                                (failed_node_dir / "package.json").as_posix()
                                if str(failed_node_dir) not in {"", "."}
                                else "package.json"
                            )
                            return packs.CodeChange(
                                kind="patch",
                                summary="auto-fix: add cross-env for NODE_ENV script on Windows",
                                writes=[packs.FileWrite(path=rel, content=json.dumps(pkg, ensure_ascii=False, indent=2) + '\n')],
                                files_changed=[rel],
                                blockers=[],
                            )

        # 2) Vite: missing index.html at project root
        if ("could not resolve entry module" in lower and "index.html" in lower) or ("entry module \"index.html\"" in lower):
            for node_dir in self._find_node_project_dirs():
                pkg_path = self.repo_root / node_dir / "package.json"
                if not pkg_path.exists():
                    continue
                try:
                    pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    continue

                deps: dict[str, str] = {}
                deps.update(dict(pkg.get("dependencies") or {}))
                deps.update(dict(pkg.get("devDependencies") or {}))
                scripts = dict(pkg.get("scripts") or {})
                looks_like_vite = ("vite" in deps) or any("vite" in str(v).lower() for v in scripts.values())
                if not looks_like_vite:
                    continue

                index_path = self.repo_root / node_dir / "index.html"
                if index_path.exists():
                    continue

                # Pick the most likely entry file.
                entry = None
                for cand in ["src/main.tsx", "src/main.ts", "src/main.jsx", "src/main.js"]:
                    if (self.repo_root / node_dir / cand).exists():
                        entry = cand
                        break
                if not entry:
                    entry = "src/main.tsx"

                html = (
                    "<!doctype html>\n"
                    "<html lang=\"en\">\n"
                    "  <head>\n"
                    "    <meta charset=\"UTF-8\" />\n"
                    "    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />\n"
                    "    <title>Vite App</title>\n"
                    "  </head>\n"
                    "  <body>\n"
                    "    <div id=\"root\"></div>\n"
                    f"    <script type=\"module\" src=\"/{entry}\"></script>\n"
                    "  </body>\n"
                    "</html>\n"
                )

                rel = (node_dir / "index.html").as_posix() if str(node_dir) not in {"", "."} else "index.html"
                return packs.CodeChange(
                    kind="patch",
                    summary=f"auto-fix: add Vite {rel}",
                    writes=[packs.FileWrite(path=rel, content=html)],
                    files_changed=[rel],
                    blockers=[],
                )

        # 3) TypeScript: common auth middleware export mismatch (scaffold bug)
        # Example:
        # src/routes/customer.routes.ts(...): error TS2614: Module '"../middleware/auth"' has no exported member 'auth'.
        # Fix by aliasing `auth` <-> `authenticate` in the module when one of them exists.
        ts_lines = [l.strip() for l in text.splitlines() if "has no exported member" in l.lower() and "error ts" in l.lower()]
        if ts_lines:
            pat = re.compile(
                r"^(?P<file>[^:(]+)\(\d+,\d+\):\s*error\s+TS(?:2305|2614):\s*Module\s+'\"(?P<spec>[^\"]+)\"'\s+has\s+no\s+exported\s+member\s+'(?P<name>[^']+)'",
                re.IGNORECASE,
            )

            def _candidate_base_dirs() -> list[Path]:
                out: list[Path] = []
                for d in self._find_node_project_dirs():
                    if d not in out:
                        out.append(d)
                if Path(".") not in out:
                    out.append(Path("."))
                return out

            def _find_importer_abs(rel_file: str) -> Optional[Path]:
                rel = (rel_file or "").replace("\\", "/").lstrip("/")
                if not rel:
                    return None
                for base in _candidate_base_dirs():
                    p = (self.repo_root / base / rel).resolve()
                    if p.exists() and p.is_file():
                        return p
                return None

            def _resolve_module_abs(importer_abs: Path, spec: str) -> Optional[Path]:
                s = (spec or "").strip()
                if not s.startswith("."):
                    return None
                target = (importer_abs.parent / s).resolve()
                candidates: list[Path] = []
                if target.suffix:
                    candidates.append(target)
                else:
                    for ext in [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".d.ts"]:
                        candidates.append(Path(str(target) + ext))
                    for ext in [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".d.ts"]:
                        candidates.append(target / f"index{ext}")
                for c in candidates:
                    if c.exists() and c.is_file():
                        try:
                            if c.resolve().is_relative_to(self.repo_root.resolve()):
                                return c.resolve()
                        except Exception:
                            return c.resolve()
                return None

            for line in ts_lines[:12]:
                m = pat.match(line)
                if not m:
                    continue
                rel_file = (m.group("file") or "").strip()
                spec = (m.group("spec") or "").strip()
                missing_name = (m.group("name") or "").strip()

                if missing_name not in {"auth", "authenticate"}:
                    continue

                importer_abs = _find_importer_abs(rel_file)
                if importer_abs is None:
                    continue
                module_abs = _resolve_module_abs(importer_abs, spec)
                if module_abs is None:
                    continue

                try:
                    mod_text = module_abs.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue

                has_auth = re.search(r"\bexport\s+(?:const|function|class)\s+auth\b", mod_text) is not None
                has_authenticate = re.search(r"\bexport\s+(?:const|function|class)\s+authenticate\b", mod_text) is not None

                if missing_name == "auth" and (not has_auth) and has_authenticate:
                    add = "\nexport const auth = authenticate;\n"
                elif missing_name == "authenticate" and (not has_authenticate) and has_auth:
                    add = "\nexport const authenticate = auth;\n"
                else:
                    continue

                if "export default" not in mod_text and has_authenticate:
                    add += "export default authenticate;\n"

                new_text = mod_text.rstrip() + add
                rel_out = module_abs.relative_to(self.repo_root.resolve()).as_posix()
                return packs.CodeChange(
                    kind="patch",
                    summary=f"auto-fix: add missing export `{missing_name}` in {rel_out}",
                    writes=[packs.FileWrite(path=rel_out, content=new_text)],
                    files_changed=[rel_out],
                    blockers=[],
                )

        return None

    def _compact_error_excerpt(self, text: str, *, max_lines: int = 60, max_chars: int = 1600) -> str:
        t = (text or "").strip()
        if not t:
            return ""
        lines = [l.rstrip() for l in t.splitlines() if l.strip()]
        if not lines:
            return ""
        tail = "\n".join(lines[-max(1, int(max_lines)) :]).strip()
        if len(tail) > max_chars:
            tail = tail[-max_chars:]
            tail = "…（已截断）…\n" + tail
        return tail.strip()

    def _validate_code_change(self, change: packs.CodeChange) -> None:
        """
        Hard, deterministic validation to prevent "writes that reference files that don't exist".

        We intentionally keep this best-effort and scoped to *relative* imports in files the model
        writes in this CodeChange, so we don't break existing repos with custom module resolution.
        """
        if not change.writes and not getattr(change, "copies", None):
            return

        exts = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".d.ts"]
        js_like = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".d.ts"}

        def norm(p: str) -> str:
            return (p or "").replace("\\", "/").lstrip("/")

        planned = {norm(w.path) for w in change.writes if (w.path or "").strip()}
        planned_lower = {p.lower() for p in planned}
        planned_text_by_path_lower = {norm(w.path).lower(): (w.content or "") for w in change.writes if (w.path or "").strip()}

        # Validate copy operations: source must exist (or be created by this change), destination must be safe.
        for c in list(getattr(change, "copies", []) or [])[:50]:
            src = norm(getattr(c, "src", "") or "")
            dst = norm(getattr(c, "dst", "") or "")
            if not src or not dst:
                raise ValueError("Missing src/dst in CodeChange.copies")
            if src.startswith(".vibe/") or src.startswith(".git/") or dst.startswith(".vibe/") or dst.startswith(".git/"):
                raise ValueError("Refusing to copy under .vibe/ or .git/")
            if src.lower() in planned_lower:
                # Copying from a file written in this change is allowed (text -> binary is still odd,
                # but we only validate existence at this layer).
                continue
            if not (self.repo_root / src).exists():
                raise ValueError(f"Copy source does not exist: {src}")

        def _read_text_best_effort(rel_path: str) -> Optional[str]:
            rel = norm(rel_path)
            if not rel:
                return None
            key = rel.lower()
            if key in planned_text_by_path_lower:
                return planned_text_by_path_lower[key]
            p = (self.repo_root / rel).resolve()
            if not p.exists() or not p.is_file():
                return None
            try:
                return p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                return None

        def extract_import_specs(text: str) -> list[str]:
            # Best-effort: cover common import styles.
            t = text or ""
            specs: list[str] = []
            pat_from = re.compile(r"\b(?:import|export)\b[\s\S]*?\bfrom\s*(['\"])([^'\"]+)\1", re.MULTILINE)
            pat_side = re.compile(r"\bimport\s*(['\"])([^'\"]+)\1", re.MULTILINE)
            pat_req = re.compile(r"\brequire\s*\(\s*(['\"])([^'\"]+)\1\s*\)", re.MULTILINE)
            pat_dyn = re.compile(r"\bimport\s*\(\s*(['\"])([^'\"]+)\1\s*\)", re.MULTILINE)
            for pat in (pat_from, pat_side, pat_req, pat_dyn):
                for m in pat.finditer(t):
                    spec = (m.group(2) or "").strip()
                    if spec:
                        specs.append(spec)
            # De-dup while preserving order.
            seen: set[str] = set()
            out: list[str] = []
            for s in specs:
                if s not in seen:
                    seen.add(s)
                    out.append(s)
            return out

        def resolve_candidates(importer_rel: str, spec: str) -> list[str]:
            importer_abs = (self.repo_root / importer_rel).resolve()
            target_abs = (importer_abs.parent / spec).resolve()
            try:
                rel = target_abs.relative_to(self.repo_root.resolve())
            except Exception:
                return []
            rel_posix = rel.as_posix()

            # If the spec already has an extension, check that exact path.
            if Path(spec).suffix:
                return [rel_posix]

            cands: list[str] = []
            for ext in exts:
                cands.append(rel_posix + ext)
            for ext in exts:
                cands.append(f"{rel_posix}/index{ext}")
            return cands

        missing: list[tuple[str, str]] = []
        for w in change.writes:
            importer_rel = norm(w.path)
            if not importer_rel:
                continue
            if Path(importer_rel).suffix.lower() not in js_like:
                continue
            for spec in extract_import_specs(w.content):
                if not spec.startswith("."):
                    continue
                cands = resolve_candidates(importer_rel, spec)
                if not cands:
                    continue
                ok = False
                for c in cands:
                    c_rel = norm(c)
                    if not c_rel:
                        continue
                    if c_rel.lower() in planned_lower:
                        ok = True
                        break
                    if (self.repo_root / c_rel).exists():
                        ok = True
                        break
                if not ok:
                    missing.append((importer_rel, spec))
                    if len(missing) >= 12:
                        break
            if len(missing) >= 12:
                break

        if missing:
            lines = "\n".join([f"- {imp}: {spec}" for imp, spec in missing[:8]])
            raise RuntimeError(
                "Missing referenced modules in CodeChange.writes. "
                "If you introduce a new relative import, you MUST also create that file in writes (or fix the import).\n"
                f"{lines}"
            )

        # Validate bare imports against package.json dependencies (best-effort).
        node_builtins = {
            "assert",
            "async_hooks",
            "buffer",
            "child_process",
            "cluster",
            "console",
            "constants",
            "crypto",
            "dgram",
            "diagnostics_channel",
            "dns",
            "domain",
            "events",
            "fs",
            "http",
            "http2",
            "https",
            "inspector",
            "module",
            "net",
            "os",
            "path",
            "perf_hooks",
            "process",
            "punycode",
            "querystring",
            "readline",
            "repl",
            "stream",
            "string_decoder",
            "timers",
            "tls",
            "tty",
            "url",
            "util",
            "v8",
            "vm",
            "worker_threads",
            "zlib",
        }

        def _base_package(spec: str) -> str:
            s = (spec or "").strip()
            s = s.split("?", 1)[0].split("#", 1)[0].strip()
            if not s:
                return ""
            if s.startswith("node:"):
                s = s[len("node:") :]
            if s.startswith("@"):
                parts = [p for p in s.split("/") if p]
                if len(parts) >= 2:
                    return f"{parts[0]}/{parts[1]}"
                return s
            return s.split("/", 1)[0].strip()

        def _is_local_alias(spec: str, *, aliases: list[str]) -> bool:
            s = (spec or "").strip()
            if not s:
                return False
            if s.startswith(("@/", "~/")):
                return True
            if s.startswith(("#", "virtual:", "data:", "http://", "https://")):
                return True
            for a in aliases:
                if a and s.startswith(a):
                    return True
            return False

        def _tsconfig_aliases(node_dir: Path) -> list[str]:
            aliases: list[str] = []
            rel = (node_dir / "tsconfig.json").as_posix() if str(node_dir) not in {"", "."} else "tsconfig.json"
            raw = _read_text_best_effort(rel)
            if not raw:
                return []
            try:
                data = json.loads(raw)
            except Exception:
                return []
            paths = (((data.get("compilerOptions") or {}).get("paths")) or {}) if isinstance(data, dict) else {}
            if not isinstance(paths, dict):
                return []
            for k in paths.keys():
                kk = str(k).replace("\\", "/").strip()
                if not kk or kk.startswith("."):
                    continue
                pre = kk.split("*", 1)[0]
                if pre:
                    aliases.append(pre)
            # Prefer longer prefixes first to reduce accidental matches.
            out: list[str] = []
            seen: set[str] = set()
            for a in sorted(aliases, key=lambda x: (-len(x), x)):
                if a not in seen:
                    seen.add(a)
                    out.append(a)
            return out

        def _find_pkg_dir(importer_rel: str) -> Optional[Path]:
            p = Path(importer_rel).parent
            while True:
                rel = (p / "package.json").as_posix() if str(p) not in {"", "."} else "package.json"
                if _read_text_best_effort(rel) is not None:
                    return p
                if str(p) in {"", "."}:
                    break
                parent = p.parent
                if parent == p:
                    break
                p = parent
            if _read_text_best_effort("package.json") is not None:
                return Path(".")
            return None

        def _deps_for_pkg_dir(pkg_dir: Path) -> set[str]:
            deps: set[str] = set()
            rels = []
            rels.append((pkg_dir / "package.json").as_posix() if str(pkg_dir) not in {"", "."} else "package.json")
            if str(pkg_dir) not in {"", "."}:
                rels.append("package.json")  # allow hoisted deps (npm/yarn workspaces)
            for rel in rels:
                raw = _read_text_best_effort(rel)
                if not raw:
                    continue
                try:
                    pkg = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(pkg, dict):
                    continue
                for k in ("dependencies", "devDependencies", "optionalDependencies"):
                    d = pkg.get(k)
                    if isinstance(d, dict):
                        deps.update({str(x).strip() for x in d.keys() if str(x).strip()})
            return deps

        missing_deps: list[tuple[str, str]] = []
        for w in change.writes:
            importer_rel = norm(w.path)
            if not importer_rel:
                continue
            if Path(importer_rel).suffix.lower() not in js_like:
                continue

            pkg_dir = _find_pkg_dir(importer_rel)
            if pkg_dir is None:
                continue
            deps = _deps_for_pkg_dir(pkg_dir)
            aliases = _tsconfig_aliases(pkg_dir)

            for spec in extract_import_specs(w.content):
                s = (spec or "").strip()
                if not s or s.startswith("."):
                    continue
                if _is_local_alias(s, aliases=aliases):
                    continue
                base = _base_package(s)
                if not base:
                    continue
                if base in node_builtins:
                    continue
                if base not in deps:
                    missing_deps.append((importer_rel, base))
                    if len(missing_deps) >= 12:
                        break
            if len(missing_deps) >= 12:
                break

        if missing_deps:
            lines = "\n".join([f"- {imp}: {pkg}" for imp, pkg in missing_deps[:8]])
            raise RuntimeError(
                "Missing npm dependencies for imports in CodeChange.writes. "
                "If you import a package, you MUST add it to the nearest package.json "
                "(dependencies/devDependencies) for that node project, or remove the import.\n"
                f"{lines}"
            )

    def _request_ownership_approval(
        self,
        *,
        actor_agent_id: str,
        rule: Any,
        writes: list[packs.FileWrite],
        activated_agents: Optional[Set[str]],
        activate_agent: Optional[Any],
        route_level: Optional[packs.RouteLevel],
        style: str,
    ) -> packs.OwnershipDecisionPack:
        owners = [o for o in list(getattr(rule, "owners", []) or []) if o in self.config.agents]
        approver_id = owners[0] if owners else ""
        if not approver_id:
            return packs.OwnershipDecisionPack(approved=True, reason="No owners configured; allowing write")

        if activate_agent and activated_agents is not None:
            try:
                activate_agent(approver_id, reason=f"ownership:{getattr(rule, 'id', 'rule')}")
            except Exception:
                pass

        diffs: list[str] = []
        file_list: list[str] = []
        for w in list(writes or [])[:24]:
            rel = (w.path or "").replace("\\", "/").lstrip("/")
            if not rel:
                continue
            file_list.append(rel)
            old_text = ""
            abs_path = (self.repo_root / rel).resolve()
            if abs_path.exists():
                try:
                    old_text = abs_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    old_text = ""
            new_text = w.content or ""
            diff_lines = difflib.unified_diff(
                old_text.splitlines(),
                new_text.splitlines(),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                lineterm="",
            )
            diffs.append("\n".join(diff_lines))

        diff_text = "\n\n".join([d for d in diffs if d.strip()]) or "(no textual diff)"
        diff_ptr = self.artifacts.put_text(diff_text, suffix=".ownership.diff", kind="ownership").to_pointer()

        if activated_agents is not None:
            try:
                self._append_guarded(
                    event=new_event(
                        agent="router",
                        type="OWNERSHIP_CHANGE_REQUESTED",
                        summary=f"Ownership approval requested: {getattr(rule, 'id', 'rule')} ({len(file_list)} file(s))",
                        branch_id=self.branch_id,
                        pointers=[diff_ptr],
                        meta={
                            "rule_id": getattr(rule, "id", ""),
                            "actor": actor_agent_id,
                            "approver": approver_id,
                            "owners": owners,
                            "files": file_list,
                            "route_level": route_level,
                            "style": style,
                        },
                    ),
                    activated_agents=activated_agents,
                )
            except Exception:
                pass

        excerpt = diff_text
        max_chars = 9000
        if len(excerpt) > max_chars:
            excerpt = excerpt[:max_chars] + "\n…(diff truncated)…\n"

        approval_system = (
            "你是仓库关键资产（契约/类型/ADR 等）的 Owner 审批人。\n"
            "任务：对本次对受保护文件的修改做拍板：批准/拒绝。\n\n"
            "输出要求：只输出符合 OwnershipDecisionPack 的 JSON：\n"
            "- approved: bool\n"
            "- reason: string\n"
            "- required_followups: string[]\n"
            "- pointers: string[]\n"
            "不要输出 markdown、不要包裹对象。"
        )
        approval_user = (
            f"变更请求：非 Owner 工种 `{actor_agent_id}` 试图修改受保护文件（rule={getattr(rule, 'id', '')}）。\n"
            f"Owner 候选：{', '.join(owners) if owners else '(none)'}；当前审批人：{approver_id}\n"
            f"文件列表（相对路径）：\n" + "\n".join([f"- {p}" for p in file_list]) + "\n\n"
            f"Diff（节选）：\n{excerpt}\n\n"
            f"DiffPointer（完整 diff 在 artifacts，可审计）：{diff_ptr}\n"
        )

        approver = self._agent(approver_id)
        msgs = self._messages_with_memory(agent_id=approver_id, system=approval_system, user=approval_user)
        decision, _ = approver.chat_json(schema=packs.OwnershipDecisionPack, messages=msgs, user=approval_user)
        decision_ptr = self.artifacts.put_json(decision.model_dump(), suffix=".ownership.decision.json", kind="ownership").to_pointer()

        if activated_agents is not None:
            try:
                self._append_guarded(
                    event=new_event(
                        agent="router",
                        type="OWNERSHIP_CHANGE_APPROVED" if decision.approved else "OWNERSHIP_CHANGE_DENIED",
                        summary=(
                            f"Ownership change approved: {getattr(rule, 'id', 'rule')}"
                            if decision.approved
                            else f"Ownership change denied: {getattr(rule, 'id', 'rule')}"
                        ),
                        branch_id=self.branch_id,
                        pointers=[decision_ptr, diff_ptr],
                        meta={
                            "rule_id": getattr(rule, "id", ""),
                            "actor": actor_agent_id,
                            "approver": approver_id,
                            "owners": owners,
                            "files": file_list,
                            "approved": bool(decision.approved),
                            "reason": str(getattr(decision, "reason", "") or "")[:400],
                            "route_level": route_level,
                            "style": style,
                        },
                    ),
                    activated_agents=activated_agents,
                )
            except Exception:
                pass

        return decision

    def _materialize_code_change(
        self,
        change: packs.CodeChange,
        *,
        actor_agent_id: str = "coder_backend",
        activated_agents: Optional[Set[str]] = None,
        activate_agent: Optional[Any] = None,
        route_level: Optional[packs.RouteLevel] = None,
        style: str = "",
        write_allowlist: Optional[List[str]] = None,
        write_denylist: Optional[List[str]] = None,
    ) -> Tuple[packs.CodeChange, List[str]]:
        def sanitize_content_for_path(path: str, content: str) -> str:
            rel = (path or "").replace("\\", "/").lstrip("/")
            if rel.endswith("package.json"):
                return self._sanitize_package_json_text(content)
            return content

        scope_allow = [_normalize_scope_pattern(p) for p in list(write_allowlist or []) if _normalize_scope_pattern(p)]
        scope_deny = [_normalize_scope_pattern(p) for p in list(write_denylist or []) if _normalize_scope_pattern(p)]

        write_pointers: List[str] = []
        write_ops: list[dict[str, str]] = []
        copy_ops: list[dict[str, str]] = []
        ptrs: list[Optional[str]] = []

        # When a write scope is active, do not allow "commit-only" changes since we can't
        # deterministically validate touched files after the fact.
        if (scope_allow or scope_deny) and change.commit_hash and not (change.writes or change.patch_pointer):
            raise RuntimeError("Write scope enforced: commit_hash-only CodeChange is not allowed; use writes/patch")
        if change.writes:
            ptrs = [None for _ in list(change.writes)]
            pending: dict[str, list[int]] = {}
            rules: dict[str, Any] = {}

            for idx, w in enumerate(list(change.writes)):
                rel = (w.path or "").replace("\\", "/").lstrip("/")
                if rel.startswith(".vibe/") or rel.startswith(".git/"):
                    raise RuntimeError(f"Refusing to write internal path: {w.path}")
                if (scope_allow or scope_deny) and not _in_write_scope(rel, allow=scope_allow, deny=scope_deny):
                    raise WriteScopeDeniedError(path=rel, allow=scope_allow, deny=scope_deny)
                try:
                    content = sanitize_content_for_path(w.path, w.content)
                    ptrs[idx] = self.toolbox.write_file(agent_id=actor_agent_id, path=w.path, content=content)
                except OwnershipDeniedError as e:
                    rid = str(getattr(e.rule, "id", "") or "ownership")
                    pending.setdefault(rid, []).append(idx)
                    rules[rid] = e.rule

            # Approval + apply protected writes as router (executor), so non-owners can't drift contracts in fix-loops.
            if pending:
                for rid, idxs in pending.items():
                    rule = rules.get(rid)
                    decision = self._request_ownership_approval(
                        actor_agent_id=actor_agent_id,
                        rule=rule,
                        writes=[change.writes[i] for i in idxs],
                        activated_agents=activated_agents,
                        activate_agent=activate_agent,
                        route_level=route_level,
                        style=style,
                    )
                    if not decision.approved:
                        reason = str(getattr(decision, "reason", "") or "").strip()
                        raise RuntimeError(f"Ownership approval denied for rule {rid}. {reason}".strip())
                    for i in idxs:
                        w = change.writes[i]
                        content = sanitize_content_for_path(w.path, w.content)
                        ptrs[i] = self.toolbox.write_file(agent_id="router", path=w.path, content=content)

            for p in ptrs:
                if p:
                    write_pointers.append(p)

            for w, p in zip(list(change.writes), list(ptrs)):
                if not p or not w.path:
                    continue
                write_ops.append({"path": (w.path or "").replace("\\", "/").lstrip("/"), "pointer": p})

            if not change.files_changed:
                change.files_changed = [w.path for w in change.writes if w.path]

        # Apply binary copy operations (e.g. workarounds for Windows shim issues).
        if getattr(change, "copies", None):
            for c in list(change.copies or [])[:80]:
                src = (c.src or "").replace("\\", "/").lstrip("/")
                dst = (c.dst or "").replace("\\", "/").lstrip("/")
                if src.startswith(".vibe/") or src.startswith(".git/") or dst.startswith(".vibe/") or dst.startswith(".git/"):
                    raise RuntimeError(f"Refusing to copy internal path: {src} -> {dst}")
                if (scope_allow or scope_deny) and dst and not _in_write_scope(dst, allow=scope_allow, deny=scope_deny):
                    raise WriteScopeDeniedError(path=dst, allow=scope_allow, deny=scope_deny)
                ptr = self.toolbox.copy_file(agent_id=actor_agent_id, src=src, dst=dst)
                write_pointers.append(ptr)
                copy_ops.append({"src": src, "dst": dst, "pointer": ptr})
                if dst and dst not in (change.files_changed or []):
                    change.files_changed = list(change.files_changed or []) + [dst]

        # Best-effort patch evidence for local file operations (writes/copies) when git is absent.
        if (write_ops or copy_ops) and not change.commit_hash and not change.patch_pointer:
            patch_ptr: Optional[str] = None
            try:
                if self.toolbox.git_is_repo(agent_id="router"):
                    diff = self.toolbox.git_diff(agent_id="router")
                    patch_ptr = diff.stdout
            except Exception:
                patch_ptr = None

            if not patch_ptr:
                patch_ptr = self.artifacts.put_json(
                    {"kind": "ops", "writes": write_ops, "copies": copy_ops},
                    suffix=".ops.json",
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
            patch_text = decode_bytes(self.artifacts.read_bytes(change.patch_pointer))
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
                    if (scope_allow or scope_deny) and not _in_write_scope(rel, allow=scope_allow, deny=scope_deny):
                        raise WriteScopeDeniedError(path=rel, allow=scope_allow, deny=scope_deny)

                patch_path = change.patch_pointer.split("@sha256:", 1)[0]
                abs_patch = (self.repo_root / patch_path).resolve()
                r = self.toolbox.run_cmd(
                    agent_id=actor_agent_id,
                    cmd=["git", "apply", "--whitespace=nowarn", str(abs_patch)],
                    cwd=self.repo_root,
                    timeout_s=600,
                )
                write_pointers.extend([r.stdout, r.stderr, r.meta])
                if r.returncode != 0:
                    err = decode_bytes(self.artifacts.read_bytes(r.stderr)).strip()
                    raise RuntimeError(f"Failed to apply patch via git apply (code={r.returncode}). {err}")

                if not change.files_changed and paths:
                    change.files_changed = sorted({p.replace("\\", "/").lstrip("/") for p in paths})

        if change.kind == "noop" or (not change.patch_pointer and not change.commit_hash and not write_pointers):
            patch_ptr = self.artifacts.put_text("mock: no code changes", suffix=".patch.txt", kind="patch").to_pointer()
            change = packs.CodeChange(kind="patch", summary=change.summary or "mock patch", patch_pointer=patch_ptr, files_changed=change.files_changed)

        return change, write_pointers

    def _materialize_code_change_with_repair(
        self,
        *,
        change: packs.CodeChange,
        actor_agent_id: str,
        actor: Any,
        actor_role: str,
        workflow_hint: str,
        activated_agents: Optional[Set[str]] = None,
        activate_agent: Optional[Any] = None,
        route_level: Optional[packs.RouteLevel] = None,
        style: str = "",
        write_allowlist: Optional[List[str]] = None,
        write_denylist: Optional[List[str]] = None,
        max_repairs: int = 2,
    ) -> Tuple[packs.CodeChange, List[str]]:
        """
        Models can return valid CodeChange JSON that still can't be applied locally (e.g. tries to write into `.vibe/`).
        Repair by re-prompting the same agent with the materialization error, so the workflow doesn't crash
        before QA/fix-loop can run.
        """
        max_repairs = max(0, min(int(max_repairs), 6))
        last_err: Optional[Exception] = None
        current = change
        for attempt in range(max_repairs + 1):
            try:
                self._validate_code_change(current)
                return self._materialize_code_change(
                    current,
                    actor_agent_id=actor_agent_id,
                    activated_agents=activated_agents,
                    activate_agent=activate_agent,
                    route_level=route_level,
                    style=style,
                    write_allowlist=write_allowlist,
                    write_denylist=write_denylist,
                )
            except Exception as e:
                last_err = e
                msg = str(e)
                repairable = any(
                    k in msg
                    for k in [
                        "Refusing to write internal path",
                        "Refusing to apply patch touching internal path",
                        "Refusing to apply patch with unsafe path",
                        "Failed to apply patch via git apply",
                        "is not in the subpath of",
                        "Missing referenced modules in CodeChange.writes",
                        "Missing npm dependencies for imports in CodeChange.writes",
                        "Ownership denied:",
                        "Ownership approval denied",
                        "Write scope denied:",
                    ]
                )
                if not repairable or attempt >= max_repairs:
                    raise

                prev = current.model_dump_json()
                repair_user = (
                    "你的上一个 CodeChange 无法在本地落地（被系统校验拒绝）。\n\n"
                    f"错误：{msg}\n\n"
                    "修复要求：\n"
                    "- 所有写入路径必须是仓库根目录的相对路径。\n"
                    "- 严禁写入 `.vibe/` 或 `.git/`（这是系统内部目录）。\n"
                    "- 如果你要写文档，请写到 `docs/...` 或 `README.md`，不要写到 `.vibe/docs/...`。\n"
                    "- 优先使用 `writes` 给出完整文件内容；如需二进制拷贝，用 `copies: [{src,dst}]`；不要依赖 patch 指针。\n"
                    "- 如果你新增了相对 import（例如 `../controllers/x`），必须在 writes 里创建对应文件（或改成指向已存在文件）。\n"
                    "- 如果你新增了外部依赖（例如 `import axios from 'axios'`），必须在对应的 `package.json`（dependencies/devDependencies）里声明它。\n"
                    "- 保持原意不变：只修复路径/可落地性问题，不要引入大重构。\n\n"
                    "请只输出符合 CodeChange schema 的 JSON（不要 markdown，不要包裹对象）。\n\n"
                    f"上一个 CodeChange（供参考）：\n{prev}\n"
                )
                repair_system = (
                    f"You are {actor_role}. Return JSON only for CodeChange with fields: "
                    "kind ('commit'|'patch'|'noop'), summary, writes? (list[{path,content}]), copies? (list[{src,dst}]), commit_hash?, patch_pointer?, files_changed[], blockers[]. "
                    "No extra keys. No markdown.\n\n"
                    "Hard rules:\n"
                    "- Do not write under `.vibe/` or `.git/`.\n"
                    "- Do not copy under `.vibe/` or `.git/`.\n"
                    "- Use only repo-root relative paths.\n"
                    "- Prefer `writes` over `patch_pointer`.\n\n"
                    f"{workflow_hint}"
                )
                repair_msgs = self._messages_with_memory(agent_id=actor_agent_id, system=repair_system, user=repair_user)
                current, _ = actor.chat_json(schema=packs.CodeChange, messages=repair_msgs, user=repair_user)

        raise RuntimeError(f"Failed to materialize CodeChange after repair attempts. Last error: {last_err}")

    def _sanitize_package_json_text(self, text: str) -> str:
        """
        Deterministic sanitization for common Windows pitfalls in npm scripts.

        Primary target:
        - Single quotes around glob patterns in scripts (e.g. `eslint 'src/**/*.ts'`), which often break on Windows
          because single quotes are treated literally.
        """

        raw = text if isinstance(text, str) else str(text or "")
        raw_stripped = raw.strip()
        if not raw_stripped.startswith("{"):
            return raw

        try:
            data = json.loads(raw)
        except Exception:
            return raw

        scripts = data.get("scripts")
        if not isinstance(scripts, dict):
            return raw

        def sanitize_script_line(line: str) -> str:
            s = line or ""
            if "'" not in s:
                return s
            # Only rewrite when the quoted segment looks like a glob/file pattern.
            if not re.search(r"\b(eslint|prettier|jest|vitest|vite|lint)\b", s, flags=re.IGNORECASE):
                return s

            def repl(m: re.Match) -> str:
                inner = m.group(1)
                return '"' + inner.replace('"', '\\"') + '"'

            return re.sub(r"'([^'\r\n]*[\*\?\[\]\{\}][^'\r\n]*)'", repl, s)

        changed = False
        for k, v in list(scripts.items()):
            if not isinstance(v, str):
                continue
            new_v = sanitize_script_line(v)
            if new_v != v:
                scripts[k] = new_v
                changed = True

        if not changed:
            return raw

        data["scripts"] = scripts
        try:
            return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        except Exception:
            return raw

    def _append_agent_memory(
        self,
        *,
        agent_id: str,
        kind: str,
        summary: str,
        pinned: list[str],
        pointers: list[str],
    ) -> None:
        view_dir = self.repo_root / ".vibe" / "views" / agent_id
        mem_path = view_dir / "memory.jsonl"
        view_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        digest = ChatDigest(
            summary=str(summary or "").strip()[:200] or "（无摘要）",
            pinned=[str(x).strip()[:200] for x in (pinned or []) if str(x).strip()][:8],
            background=[],
            open_questions=[],
        )
        record = MemoryRecord(
            ts=ts,
            agent_id=agent_id,
            kind=kind,  # type: ignore[arg-type]
            digest=digest,
            pointers=list(pointers or [])[:16],
        )
        append_memory_record(mem_path, record)

    def _append_agent_lesson(self, *, agent_id: str, summary: str, pinned: list[str], pointers: list[str]) -> None:
        """
        "Eat a pit, grow wiser": persist a short, pointer-grounded lesson for future prompts.
        """

        self._append_agent_memory(agent_id=agent_id, kind="lesson", summary=summary, pinned=pinned, pointers=pointers)

    def _autofix_lesson_text(self, *, change_summary: str) -> tuple[str, list[str]]:
        s = (change_summary or "").strip()
        low = s.lower()
        if "normalize eslint glob quoting" in low:
            return (
                "Windows 下 npm scripts 的单引号不会被当作引号剥离，可能导致 ESLint glob 变成字面量而找不到文件。",
                [
                    "npm scripts 里的 glob 尽量用双引号（\"...\") 或不加引号，避免单引号（'...'）。",
                    "当 ESLint 报 `No files matching the pattern \"'...\"`，优先怀疑是引号问题而不是目录缺失。",
                ],
            )
        if "add missing devdependency" in low:
            return (
                "Node 项目脚本里调用的工具（tsc/jest/eslint 等）必须在 package.json 依赖中可用，否则会在 CI/新机器上失败。",
                [
                    "如果 scripts 里直接用 `tsc`，确保 `devDependencies.typescript` 存在。",
                    "如果 scripts 里直接用 `jest`/`eslint`/`vitest`，确保对应包在 devDependencies。",
                ],
            )
        if "add cross-env" in low and "node_env" in low:
            return (
                "Windows cmd.exe 不支持 `NODE_ENV=...` 这种写法；需要 cross-env 或改写脚本。",
                [
                    "跨平台设置环境变量：用 `cross-env NODE_ENV=production ...`。",
                    "避免在 npm scripts 里直接写 `NODE_ENV=...`（除非明确只跑在类 Unix shell）。",
                ],
            )
        if "add eslint config" in low:
            return (
                "ESLint 缺少配置文件会直接失败；如果要启用 lint，需要一份可用的 ESLint 配置。",
                [
                    "当 ESLint 报 missing config：补 `.eslintrc.*` 或 `eslint.config.*`。",
                    "TypeScript 项目 lint 通常还需要 `@typescript-eslint/parser` + plugin。",
                ],
            )
        if "node_modules/.bin" in low and ".cmd" in low and "prefer" in low:
            return (
                "Windows/npm 的 `node_modules/.bin` 入口通常是 `.cmd`/`.ps1`；`.exe` 可能不存在或是 0 字节占位 shim。",
                [
                    "不要在脚本里硬编码/校验 `.bin/<tool>.exe`；优先调用 `.cmd` 或 `npm exec -- <tool>`/`npx <tool>`。",
                    "如果脚本同时有 `.bin` 与 `bin/` 手动回退：只修 `.bin` 那段，避免误改 `bin/` 回退。",
                ],
            )
        return ("", [])

    def _determine_test_commands(self, *, profile: str) -> List[str]:
        if os.getenv("VIBE_MOCK_MODE", "").strip() == "1":
            return ["mock"]

        node_dirs = self._find_node_project_dirs()
        is_node = bool(node_dirs)
        tests_dir = self.repo_root / "tests"
        has_py_tests = False
        if tests_dir.exists():
            try:
                has_py_tests = any(p.is_file() for p in tests_dir.rglob("test*.py"))
            except Exception:
                has_py_tests = False

        # Python detection should be conservative: many Node/TS repos have a `tests/` folder.
        # Only treat a repo as Python when we see real Python indicators.
        py_markers = [
            "pyproject.toml",
            "requirements.txt",
            "setup.py",
            "Pipfile",
            "poetry.lock",
            "uv.lock",
            ".python-version",
        ]
        has_py_markers = any((self.repo_root / m).exists() for m in py_markers)
        is_py = has_py_markers or has_py_tests

        p = profile.strip().lower()
        if p == "smoke":
            if is_py:
                return ["python -m compileall ."]
            if is_node:
                node_dir = node_dirs[0]
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
                if "test" in scripts and self._node_has_tests(node_dir):
                    return [self._shell_cmd_in_dir(rel_dir=node_dir, cmd=f"{pm} test")]
                return []
            return []

        if is_py:
            # Prefer pytest when available and the project appears to have tests.
            if has_py_tests and self._python_has_module("pytest"):
                return ["python -m compileall .", "pytest -q"]
            return ["python -m compileall .", "python -m unittest -q"]
        if is_node:
            cmds: list[str] = []
            for node_dir in node_dirs:
                pm = self._package_manager(node_dir)
                scripts: dict[str, str] = {}
                try:
                    pkg = json.loads((self.repo_root / node_dir / "package.json").read_text(encoding="utf-8", errors="replace"))
                    scripts = dict(pkg.get("scripts") or {})
                except Exception:
                    scripts = {}

                has_node_tests = self._node_has_tests(node_dir)

                local: list[str] = []
                if p == "full":
                    if "build" in scripts:
                        local.append(self._shell_cmd_in_dir(rel_dir=node_dir, cmd=f"{pm} run build"))
                    if "lint" in scripts:
                        local.append(self._shell_cmd_in_dir(rel_dir=node_dir, cmd=f"{pm} run lint"))
                    if "test" in scripts and has_node_tests:
                        local.append(self._shell_cmd_in_dir(rel_dir=node_dir, cmd=f"{pm} test"))
                else:
                    if "lint" in scripts:
                        local.append(self._shell_cmd_in_dir(rel_dir=node_dir, cmd=f"{pm} run lint"))
                    if "test" in scripts and has_node_tests:
                        local.append(self._shell_cmd_in_dir(rel_dir=node_dir, cmd=f"{pm} test"))
                    if not local and "build" in scripts:
                        local.append(self._shell_cmd_in_dir(rel_dir=node_dir, cmd=f"{pm} run build"))

                cmds.extend(local)
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
        # Report what we actually executed (fail-fast). This reduces noise and helps
        # fix-loop converge on the *first causal* failure.
        report_cmds: List[str] = []

        # Best-effort: for Node projects, ensure deps exist before running build/lint/test.
        # This avoids failures like "cannot find module" on fresh checkouts.
        try:
            node_dirs: list[Path] = []
            for c in cmds:
                if not isinstance(c, str):
                    continue
                lower = c.lower()
                if not re.search(r"\b(?:npm|pnpm|yarn)\b", lower):
                    continue
                node_dirs.append(self._shell_cd_dir(c))

            pre_cmds: list[str] = []
            seen: set[str] = set()
            for node_dir in node_dirs:
                key = node_dir.as_posix() or "."
                if key in seen:
                    continue
                seen.add(key)

                needs, _reason = self._node_install_needed(node_dir)
                if not needs:
                    continue

                pm = self._package_manager(node_dir)
                install_cmd = self._shell_cmd_in_dir(rel_dir=node_dir, cmd=f"{pm} install")
                pre_cmds.append(install_cmd)

                r = self.toolbox.run_cmd(agent_id="qa", cmd=install_cmd, cwd=self.repo_root, timeout_s=3600)
                passed = r.returncode == 0
                results.append(
                    packs.TestResult(
                        command=install_cmd,
                        returncode=r.returncode,
                        passed=passed,
                        stdout=r.stdout,
                        stderr=r.stderr,
                        meta=r.meta,
                    )
                )
                pointers.extend([r.stdout, r.stderr, r.meta])
                if not passed:
                    stderr_tail = self._compact_error_excerpt(self._artifact_tail_text(r.stderr, max_bytes=12000))
                    stdout_tail = self._compact_error_excerpt(self._artifact_tail_text(r.stdout, max_bytes=12000))
                    excerpt = stderr_tail or stdout_tail
                    if excerpt:
                        blockers.append(f"Command failed: {install_cmd}\n\n{excerpt}")
                    else:
                        blockers.append(f"Command failed: {install_cmd}")
                    report_cmds = [x.command for x in results]
                    return packs.TestReport(commands=report_cmds, results=results, passed=False, blockers=blockers, pointers=pointers)

            if pre_cmds:
                report_cmds.extend(pre_cmds)
        except Exception:
            pass

        for cmd in cmds:
            r = self.toolbox.run_cmd(agent_id="qa", cmd=cmd, cwd=self.repo_root, timeout_s=1800)
            passed = r.returncode == 0
            results.append(
                packs.TestResult(command=cmd, returncode=r.returncode, passed=passed, stdout=r.stdout, stderr=r.stderr, meta=r.meta)
            )
            pointers.extend([r.stdout, r.stderr, r.meta])
            report_cmds.append(cmd)
            if not passed:
                stderr_tail = self._compact_error_excerpt(self._artifact_tail_text(r.stderr, max_bytes=12000))
                stdout_tail = self._compact_error_excerpt(self._artifact_tail_text(r.stdout, max_bytes=12000))
                excerpt = stderr_tail or stdout_tail
                if excerpt:
                    blockers.append(f"Command failed: {cmd}\n\n{excerpt}")
                else:
                    blockers.append(f"Command failed: {cmd}")
                break

        return packs.TestReport(commands=report_cmds, results=results, passed=all(x.passed for x in results), blockers=blockers, pointers=pointers)

    def _find_resume_checkpoint(self, *, task_evt: LedgerEvent, task_text: str) -> Optional[Checkpoint]:
        """
        Find the latest non-green checkpoint for the current branch+task.

        This enables "continue fixing" runs (resume) without re-running PM/ADR/PLAN
        and re-implementing tasks from scratch.
        """
        try:
            checkpoints = list(self.checkpoints.list())
        except Exception:
            return None
        if not checkpoints:
            return None

        # 1) Exact match (preferred): task_id + branch_id.
        for cp in reversed(checkpoints):
            if cp.green:
                continue
            meta = dict(cp.meta or {})
            if str(meta.get("branch_id") or "main") != self.branch_id:
                continue
            if str(meta.get("task_id") or "") == str(task_evt.id or ""):
                return cp

        # 2) Heuristic fallback: match by label similarity (older checkpoints may not have task_id).
        head = (task_text or "").strip().splitlines()[0][:120].lower()
        if not head:
            return None
        for cp in reversed(checkpoints):
            if cp.green:
                continue
            meta = dict(cp.meta or {})
            if str(meta.get("branch_id") or "main") != self.branch_id:
                continue
            reason = str(meta.get("reason") or "").strip()
            if reason not in AUTO_RESUME_REASONS:
                continue
            label = (cp.label or "").strip().lower()
            if not label:
                continue
            if head in label or label in head:
                return cp
            try:
                if difflib.SequenceMatcher(None, head, label).ratio() >= 0.72:
                    return cp
            except Exception:
                continue
        return None

    def run(
        self,
        *,
        task_id: Optional[str] = None,
        route: Optional[str] = None,
        style: Optional[str] = None,
        resume: bool = True,
    ) -> RunResult:
        task_evt = self._find_task(task_id)
        task_text = str(task_evt.meta.get("text") or task_evt.summary)

        resume_cp: Optional[Checkpoint] = None
        if resume and self.policy.mode != "chat_only":
            try:
                resume_cp = self._find_resume_checkpoint(task_evt=task_evt, task_text=task_text)
            except Exception:
                resume_cp = None

        style_seed = style or os.getenv("VIBE_STYLE") or getattr(self.config.behavior, "style", "balanced")
        if (not style) and (not os.getenv("VIBE_STYLE")) and resume_cp is not None:
            prev_style = str((resume_cp.meta or {}).get("style") or "").strip()
            if prev_style:
                style_seed = prev_style

        resolved_style = normalize_style(style_seed)
        workflow_hint = style_workflow_hint(resolved_style)
        mock_mode = os.getenv("VIBE_MOCK_MODE", "").strip() == "1"

        diff = self._git_diff_stats_best_effort()
        risks = detect_risks(task_text, diff=diff)
        requested_level = route
        if resume_cp is not None and (not route or str(route).strip().lower() in {"auto", "default"}):
            prev_level = str((resume_cp.meta or {}).get("route_level") or "").strip().upper()
            if prev_level in {"L0", "L1", "L2", "L3", "L4"}:
                requested_level = prev_level
        decision = decide_route(
            task_text=task_text,
            diff=diff,
            recent_test_fail_count=self._recent_test_fail_count(),
            requested_level=requested_level,
        )
        requested_route_level = decision.route_level
        route_level = requested_route_level
        route_reasons = list(decision.reasons or [])

        resume_from: Optional[str] = None
        resume_reason: Optional[str] = None
        if resume_cp is not None:
            meta = dict(resume_cp.meta or {})
            if not resume_cp.green and str(meta.get("reason") or "").strip() in AUTO_RESUME_REASONS:
                resume_from = resume_cp.id
                resume_reason = str(meta.get("reason") or "").strip()

        agent_pool_list = self._agent_pool_for_route(route_level)
        agent_pool: Set[str] = set(agent_pool_list)

        activated_agents_list = self._required_agents_for_route(route_level, risks=risks)
        for a in activated_agents_list:
            if a not in agent_pool:
                agent_pool.add(a)
                agent_pool_list.append(a)
        activated_agents: Set[str] = set(activated_agents_list)

        # Route-aware planning/execution caps: higher strategies expand more and may require
        # more plan tasks, but we still keep hard bounds to avoid runaway runs.
        base_cap = {"L0": 3, "L1": 6, "L2": 8, "L3": 10, "L4": 12}.get(str(route_level), 6)
        if resolved_style == "free":
            base_cap = base_cap + 1
        if resolved_style == "detailed":
            base_cap = max(3, base_cap - 1)
        max_plan_tasks = max(3, min(int(base_cap), 16))
        max_implement_tasks = max_plan_tasks

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
                    "requested_route_level": requested_route_level,
                    "reasons": route_reasons,
                    "style": resolved_style,
                    "resume_from": resume_from,
                    "resume_reason": resume_reason,
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
                meta={
                    "route_level": route_level,
                    "requested_route_level": requested_route_level,
                    "agent_pool": agent_pool_list,
                    "agents": activated_agents_list,
                    "style": resolved_style,
                    "resume_from": resume_from,
                },
            ),
            activated_agents=activated_agents,
        )

        def activate_agent(agent_id: str, *, reason: str) -> None:
            nonlocal activated_agents_list, activated_agents, agent_pool_list, agent_pool
            a = (agent_id or "").strip()
            if not a or a in activated_agents:
                return
            if a not in self.config.agents:
                return
            activated_agents.add(a)
            activated_agents_list.append(a)
            if a not in agent_pool:
                agent_pool.add(a)
                agent_pool_list.append(a)
            self._append_guarded(
                event=new_event(
                    agent="router",
                    type="AGENTS_ACTIVATED",
                    summary=f"Activated agent {a}",
                    branch_id=self.branch_id,
                    pointers=[],
                    meta={
                        "route_level": route_level,
                        "requested_route_level": requested_route_level,
                        "reason": reason,
                        "added": [a],
                        "agents": activated_agents_list,
                        "style": resolved_style,
                    },
                ),
                activated_agents=activated_agents,
            )

        # Keep manifests/index fresh so agents can ground answers in repo facts.
        try:
            self.toolbox.scan_repo(agent_id="router", reason="workflow")
        except PolicyDeniedError:
            pass
        except Exception:
            pass

        router = self._agent("router")
        implementation_lead = self._agent("implementation_lead") if "implementation_lead" in self.config.agents else None
        pm = self._agent("pm") if "pm" in self.config.agents else None
        req_analyst = self._agent("requirements_analyst") if "requirements_analyst" in self.config.agents else None
        architect = self._agent("architect") if "architect" in self.config.agents else None
        api_confirm = self._agent("api_confirm") if "api_confirm" in self.config.agents else None
        web_info = self._agent("web_info") if "web_info" in self.config.agents else None
        env_engineer = self._agent("env_engineer") if "env_engineer" in self.config.agents else None
        coder_backend = self._agent("coder_backend")
        coder_frontend = self._agent("coder_frontend") if "coder_frontend" in self.config.agents else None
        integrator = self._agent("integration_engineer") if "integration_engineer" in self.config.agents else None
        reviewer = self._agent("code_reviewer") if "code_reviewer" in self.config.agents else None
        security = self._agent("security") if "security" in self.config.agents else None
        compliance = self._agent("compliance") if "compliance" in self.config.agents else None
        performance = self._agent("performance") if "performance" in self.config.agents else None
        data_engineer = self._agent("data_engineer") if "data_engineer" in self.config.agents else None
        devops = self._agent("devops") if "devops" in self.config.agents else None
        doc_writer = self._agent("doc_writer") if "doc_writer" in self.config.agents else None
        release_manager = self._agent("release_manager") if "release_manager" in self.config.agents else None
        support_engineer = self._agent("support_engineer") if "support_engineer" in self.config.agents else None
        ops_engineer = self._agent("ops_engineer") if "ops_engineer" in self.config.agents else None
        specialist = self._agent("specialist") if "specialist" in self.config.agents else None

        ctx, ctx_excerpts = self._build_context_packet(task_evt=task_evt)
        self._append_guarded(
            event=new_event(
                agent="router",
                type="CONTEXT_PACKET_BUILT",
                summary="Built ContextPacket",
                branch_id=self.branch_id,
                pointers=ctx.repo_pointers,
                meta={"route_level": route_level, "style": resolved_style, "resume_from": resume_from},
            ),
            activated_agents=activated_agents,
        )

        # Tooling probe (PATH detection): prevent agents from choosing architectures that
        # require missing global CLIs. This is especially important for "create a website"
        # tasks where a model might pick e.g. Hugo by default.
        tooling_ptr, tooling_summary, tooling_available, tooling_missing = self._tooling_probe()
        if tooling_ptr:
            try:
                ctx.log_pointers.append(tooling_ptr)
            except Exception:
                pass
        if tooling_missing:
            missing_txt = ", ".join(tooling_missing[:8])
            try:
                ctx.constraints.append(
                    f"ToolingProbe：本机 PATH 未检测到这些命令：{missing_txt}。不要选择依赖这些全局 CLI 的技术栈；优先使用可通过 npm/pip 本地依赖落地的方案（详见 {tooling_ptr or 'tooling probe'}）。"
                )
            except Exception:
                pass
        if tooling_ptr:
            self._append_guarded(
                event=new_event(
                    agent="router",
                    type="ENV_PROBED",
                    summary=f"Tooling probe: {tooling_summary}",
                    branch_id=self.branch_id,
                    pointers=[tooling_ptr],
                    meta={"route_level": route_level, "style": resolved_style, "resume_from": resume_from},
                ),
                activated_agents=activated_agents,
            )

        wc_ptr: Optional[str] = None
        wc_summary: str = ""
        try:
            wc_ptr, wc_summary, wc_excerpt = self._write_workspace_contract(
                route_level=route_level,
                style=resolved_style,
                tooling_ptr=tooling_ptr,
                tooling_available=tooling_available,
                tooling_missing=tooling_missing,
            )
            if wc_ptr:
                ctx.repo_pointers.append(wc_ptr)
                try:
                    ctx.constraints.append(f"WorkspaceContract：{wc_summary}（详见 {wc_ptr}）。")
                except Exception:
                    pass
                if str(wc_excerpt or "").strip():
                    extra = f"<<< {wc_ptr} >>>\n{wc_excerpt.strip()}\n"
                    if ctx_excerpts:
                        ctx_excerpts = (ctx_excerpts.rstrip() + "\n\nWorkspaceContract:\n" + extra).strip()
                    else:
                        ctx_excerpts = ("WorkspaceContract:\n" + extra).strip()

                self._append_guarded(
                    event=new_event(
                        agent="router",
                        type="WORKSPACE_CONTRACT_BUILT",
                        summary=f"WorkspaceContract built ({wc_summary})",
                        branch_id=self.branch_id,
                        pointers=[wc_ptr],
                        meta={"route_level": route_level, "style": resolved_style},
                    ),
                    activated_agents=activated_agents,
                )
        except Exception:
            wc_ptr = None
            wc_summary = ""

        req: packs.RequirementPack | None = None
        req_ptr: Optional[str] = None
        intent: packs.IntentExpansionPack | None = None
        intent_ptr: Optional[str] = None
        usecases: Optional[packs.UseCasePack] = None
        usecases_ptr: Optional[str] = None
        decisions: Optional[packs.DecisionPack] = None
        decisions_ptr: Optional[str] = None
        contract: Optional[packs.ContractPack] = None
        contract_ptr: Optional[str] = None
        plan: Optional[packs.Plan] = None
        plan_ptr: Optional[str] = None
        resume_impl_blueprint_candidate: Optional[packs.ImplementationBlueprint] = None
        resume_impl_blueprint_ptr: Optional[str] = None
        resume_blockers: list[str] = []
        resume_replan_trigger: str = ""

        resume_mode = bool(resume_from)
        resume_replan_mode = resume_reason == "replan_required"
        resume_skip_implement = resume_mode and not resume_replan_mode
        if resume_mode:
            self._append_guarded(
                event=new_event(
                    agent="router",
                    type="STATE_TRANSITION",
                    summary=f"Resuming from checkpoint {resume_from}",
                    branch_id=self.branch_id,
                    pointers=[],
                    meta={"phase": "resume", "from_checkpoint": resume_from, "route_level": route_level, "style": resolved_style},
                ),
                activated_agents=activated_agents,
            )
            try:
                meta = dict((resume_cp.meta or {}) if resume_cp is not None else {})
            except Exception:
                meta = {}

            def _load(ptr: Optional[str], schema):
                if not ptr:
                    return None
                try:
                    raw = self.artifacts.read_bytes(ptr)
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                    return schema.model_validate(data)
                except Exception:
                    return None

            req_ptr = str(meta.get("req_ptr") or "").strip() or None
            intent_ptr = str(meta.get("intent_ptr") or "").strip() or None
            plan_ptr = str(meta.get("plan_ptr") or "").strip() or None
            usecases_ptr = str(meta.get("usecases_ptr") or "").strip() or None
            decisions_ptr = str(meta.get("decisions_ptr") or "").strip() or None
            contract_ptr = str(meta.get("contract_ptr") or "").strip() or None
            resume_impl_blueprint_ptr = str(meta.get("impl_blueprint_ptr") or "").strip() or None
            resume_blockers = [str(x).strip() for x in list(meta.get("blockers") or []) if str(x).strip()][:12]
            resume_replan_trigger = str(meta.get("replan_trigger") or "").strip()[:240]

            req = _load(req_ptr, packs.RequirementPack) if req_ptr else None
            intent = _load(intent_ptr, packs.IntentExpansionPack) if intent_ptr else None
            plan = _load(plan_ptr, packs.Plan) if plan_ptr else None
            usecases = _load(usecases_ptr, packs.UseCasePack) if usecases_ptr else None
            decisions = _load(decisions_ptr, packs.DecisionPack) if decisions_ptr else None
            contract = _load(contract_ptr, packs.ContractPack) if contract_ptr else None
            resume_impl_blueprint_candidate = (
                _load(resume_impl_blueprint_ptr, packs.ImplementationBlueprint) if resume_impl_blueprint_ptr else None
            )

            if req is None and route_level != "L0":
                req = packs.RequirementPack(
                    summary=(task_text.strip().splitlines()[0][:120] or "Resume task"),
                    acceptance=[],
                    non_goals=[],
                    constraints=["Assume: resuming from previous non-green checkpoint; reusing prior spec/plan unless updated."],
                )
            if plan is None:
                # Resume should not re-run PLAN/IMPLEMENT. Keep an empty Plan for
                # downstream prompts (envspec/review) without triggering work.
                plan = packs.Plan(tasks=[])

        doctor_ptr: Optional[str] = None
        if route_level != "L0":
            try:
                doctor_ptr, doctor_summary = self._doctor_preflight(max_findings=6)
                if doctor_ptr:
                    # Feed doctor findings into the shared ContextPacket (short, pointer-backed).
                    ctx.log_pointers.append(doctor_ptr)
                    if doctor_summary:
                        ctx.constraints.append(f"Doctor预检：{doctor_summary}（详见 {doctor_ptr}）")
                    self._append_guarded(
                        event=new_event(
                            agent="router",
                            type="INCIDENT_CREATED",
                            summary="Doctor preflight findings",
                            branch_id=self.branch_id,
                            pointers=[doctor_ptr],
                            meta={
                                "category": "doctor_preflight",
                                "route_level": route_level,
                                "style": resolved_style,
                                "task_id": task_evt.id,
                                "resume_from": resume_from,
                            },
                        ),
                        activated_agents=activated_agents,
                    )
            except PolicyDeniedError:
                pass
            except Exception:
                pass

        if (not resume_mode) and route_level != "L0":
            activate_agent("pm", reason="gate:requirements")
            if not pm:
                raise RuntimeError("pm is required for L1+ routes")
            pm_user = f"Task:\n{task_text}\n\nContextPacket:\n{ctx.model_dump_json()}"
            if ctx_excerpts:
                pm_user = f"{pm_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
            pm_msgs = self._messages_with_memory(
                agent_id="pm",
                system=(
                    "你是乙方产品经理（PM）：目标是把需求推进到“能交付、能跑起来、能验证”的状态。\n"
                    "只输出 JSON（不要 markdown），并严格匹配 RequirementPack schema："
                    "{summary: string, acceptance: string[], non_goals: string[], constraints: string[]}。\n"
                    "不要额外 key；不要最外层包一层对象。\n\n"
                    "交付导向规则：\n"
                    "- acceptance 必须包含可运行/可验证的交付标准（例如 README 写清楚安装/启动/最小验证步骤）。\n"
                    "- 如果需求暗示“实时/价格/行情/外部数据”，默认规划可配置的真实数据源；若缺少 key/网络不可达，必须明确回退 mock，并在接口/界面标注 source=mock。\n"
                    "- 信息不足时，不要停在追问：先做合理默认假设，并用 constraints 里的 'Assume:' 写出来。\n\n"
                    f"{workflow_hint}"
                ),
                user=pm_user,
            )
            req, _req_meta = pm.chat_json(
                schema=packs.RequirementPack,
                messages=pm_msgs,
                user=task_text,
            )
            req = augment_requirement_pack(req, task_text=task_text)
            try:
                req_ptr = self.artifacts.put_json(req.model_dump(), suffix=".req.json", kind="req").to_pointer()
                self._append_guarded(
                    event=new_event(
                        agent="pm",
                        type="REQ_UPDATED",
                        summary="RequirementPack updated",
                        branch_id=self.branch_id,
                        pointers=[req_ptr],
                        meta={"task_id": task_evt.id, "route_level": route_level, "style": resolved_style},
                    ),
                    activated_agents=activated_agents,
                )
            except Exception:
                req_ptr = None
            self._append_guarded(
                event=new_event(
                    agent="pm",
                    type="AC_DEFINED",
                    summary="Acceptance criteria defined",
                    branch_id=self.branch_id,
                    pointers=[],
                    meta={"acceptance": req.acceptance, "route_level": route_level, "style": resolved_style, "task_id": task_evt.id},
                ),
                activated_agents=activated_agents,
            )

        if (not resume_mode) and route_level != "L0" and "intent_expander" in self.config.agents:
            activate_agent("intent_expander", reason="gate:intent_expansion")
            try:
                expander = self._agent("intent_expander")
                ie_user = (
                    f"RouteLevel: {route_level}\nStyle: {resolved_style}\n\n"
                    f"Task:\n{task_text}\n\n"
                    f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                    f"ContextPacket:\n{ctx.model_dump_json()}"
                )
                if ctx_excerpts:
                    ie_user = f"{ie_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
                ie_msgs = self._messages_with_memory(
                    agent_id="intent_expander",
                    system=(
                        "你是意图展开器（intent_expander）。目标：在不频繁追问用户的前提下，把需求扩展为更完整、可交付的功能/质量清单。\n"
                        "只输出 JSON（不要 markdown），并严格匹配 IntentExpansionPack schema：\n"
                        "{summary: string, route_level: 'L0'|'L1'|'L2'|'L3'|'L4', assumptions: string[], defaults: object, "
                        "feature_backlog: [{id,title,description,priority,acceptance,tags}], open_questions: string[], constraints: string[], non_goals: string[]}。\n"
                        "不要额外 key；不要最外层包一层对象。\n\n"
                        "展开强度规则（route_level 越高越完整，功能更丰富更复杂）：\n"
                        "- L0(快速/草稿)：最少展开；只给 3-6 条关键假设/最小闭环。\n"
                        "- L1(简单MVP)：补齐同类产品的常见模块（CRUD/权限/日志/README/最小验证）。\n"
                        "- L2(多模块MVP)：补齐契约/模块边界/用例/错误处理/集成验证。\n"
                        "- L3(可发布)：补齐可复现运行、配置、回归测试、基础安全、发布说明。\n"
                        "- L4(生产级)：补齐监控/告警/性能/合规/运维Runbook/回滚与审计。\n\n"
                        "约束：\n"
                        "- 不要声称“已创建/已实现”；这里只是提出 backlog/假设。\n"
                        "- assumptions 用 'Assume:' 开头写默认假设（例如技术栈、数据源、部署方式）。\n"
                        "- feature_backlog 要可执行、可验收；priority 用 must/should/could。\n"
                        "- 如必须追问，放到 open_questions（<=6），其余都用合理默认假设前进。\n\n"
                        f"{workflow_hint}"
                    ),
                    user=ie_user,
                )
                intent, _ = expander.chat_json(schema=packs.IntentExpansionPack, messages=ie_msgs, user=ie_user)
                # Ensure route_level is consistent with the selected route (models may echo a different one).
                try:
                    intent = intent.model_copy(update={"route_level": route_level})
                except Exception:
                    pass
                intent_ptr = self.artifacts.put_json(intent.model_dump(), suffix=".intent.json", kind="intent").to_pointer()
                self._append_guarded(
                    event=new_event(
                        agent="intent_expander",
                        type="INTENT_EXPANDED",
                        summary="Intent expanded",
                        branch_id=self.branch_id,
                        pointers=[intent_ptr],
                        meta={"route_level": route_level, "style": resolved_style, "task_id": task_evt.id},
                    ),
                    activated_agents=activated_agents,
                )
                try:
                    ctx.constraints.append(f"IntentExpansion：{(intent.summary or '').strip()[:180]}（详见 {intent_ptr}）。")
                except Exception:
                    pass
            except Exception:
                intent = None
                intent_ptr = None

        needs_arch = route_level in {"L2", "L3", "L4"}
        try:
            if route_level == "L1" and any("工程骨架提示" in str(c) for c in (ctx.constraints or [])):
                needs_arch = True
        except Exception:
            needs_arch = needs_arch

        if (not resume_mode) and route_level in {"L2", "L3", "L4"}:
            activate_agent("requirements_analyst", reason="gate:usecases")
            if req_analyst is None:
                raise RuntimeError("requirements_analyst is required for L2+ routes")
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
                    meta={"route_level": route_level, "style": resolved_style, "task_id": task_evt.id},
                ),
                activated_agents=activated_agents,
            )

        if (not resume_mode) and needs_arch:
            activate_agent("architect", reason="gate:architecture")
            if architect is None:
                raise RuntimeError("architect is required for architecture gate")
            adr_user = (
                f"Task:\n{task_text}\n\nRequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                f"UseCasePack:\n{usecases.model_dump_json() if usecases is not None else '{}'}\n\nContextPacket:\n{ctx.model_dump_json()}"
            )
            if ctx_excerpts:
                adr_user = f"{adr_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
            adr_msgs = self._messages_with_memory(
                agent_id="architect",
                system=(
                    "你是架构师（architect）。你要先把“整体架构 + 跨角色共享约定”讲清楚，避免后续靠 QA 失败倒逼修。\n"
                    "只输出 JSON（不要 markdown），并严格匹配 DecisionPack schema："
                    "{adrs: list[object], shared_context: object}。\n"
                    "规则：\n"
                    "- adrs：至少 1 条 ADR-lite；每条至少包含 title/context/decision/consequences。\n"
                    "- shared_context：必须包含以下 key（即使为空也要给出）：repo_layout、commands、env_vars。\n"
                    "  - repo_layout：说明关键目录（如 client/backend）、包管理与构建产物目录等。\n"
                    "  - commands：给出安装/开发/构建/测试的推荐命令（存在则引用现有脚本；不存在则说明需要补齐）。\n"
                    "  - env_vars：列出需要的环境变量（如 API key、DB 连接等）。\n"
                    "- 不要发散到实现细节；目标是让 Router 能拆任务、Coder 能落地、QA 能验证。\n"
                    "不要额外 key；不要最外层包一层对象。\n\n"
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
                    meta={"route_level": route_level, "style": resolved_style, "task_id": task_evt.id},
                ),
                activated_agents=activated_agents,
            )

            # Completeness gate: require a minimal shared_context contract so coders stay aligned.
            missing_keys: list[str] = []
            try:
                sc = dict(decisions.shared_context or {})
            except Exception:
                sc = {}
            for k in ["repo_layout", "commands", "env_vars"]:
                if k not in sc:
                    missing_keys.append(k)
            if not list(decisions.adrs or []):
                missing_keys.append("adrs")

            if missing_keys:
                try:
                    adr_user2 = (
                        f"{adr_user}\n\nPreviousDecisionPack:\n{decisions.model_dump_json()}\n\n"
                        f"MissingFields:\n- " + "\n- ".join(missing_keys[:8]) + "\n\n"
                        "Request:\n- 仅补齐缺失字段；不要删除已有内容。\n- shared_context 必须包含 repo_layout/commands/env_vars。\n"
                    )
                    adr_msgs2 = self._messages_with_memory(
                        agent_id="architect",
                        system=(
                            "你是架构师（architect）。你需要补齐上一轮 DecisionPack 的缺失字段。\n"
                            "只输出 DecisionPack JSON；不要额外 key；不要 markdown。\n\n"
                            f"{workflow_hint}"
                        ),
                        user=adr_user2,
                    )
                    decisions2, _ = architect.chat_json(schema=packs.DecisionPack, messages=adr_msgs2, user=adr_user2)
                    decisions = decisions2
                    decisions_ptr = self.artifacts.put_json(decisions.model_dump(), suffix=".adr.json", kind="adr").to_pointer()
                    self._append_guarded(
                        event=new_event(
                            agent="architect",
                            type="ARCH_UPDATED",
                            summary="Architecture clarified",
                            branch_id=self.branch_id,
                            pointers=[decisions_ptr],
                            meta={"route_level": route_level, "style": resolved_style, "missing_fields": missing_keys},
                        ),
                        activated_agents=activated_agents,
                    )
                except Exception:
                    pass

        if (not resume_mode) and route_level in {"L2", "L3", "L4"} and (risks.contract_change or risks.touches_external_api):
            web_info_ptr: Optional[str] = None
            if web_info is not None:
                activate_agent("web_info", reason="gate:web_info")
                wi_user = (
                    "你是 web_info：你必须使用联网搜索来做“事实查证”，并只输出 WebInfoPack JSON（不要 markdown）。\n"
                    "查证目标：\n"
                    "- 如果需求涉及外部 API/SDK/endpoint/base_url/model 名称/兼容模式：给出最关键的正确值与来源链接。\n"
                    "- 如果你不确定或无法查到：把 confidence 设为 low，并在 notes 里说明缺口。\n"
                    "规则：\n"
                    "- sources.url 必须是完整 URL（不要编造）。\n"
                    "- 不要输出额外 key；不要套外层对象。\n\n"
                    f"Task:\n{task_text}\n\nRequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n"
                )
                try:
                    wi_msgs = self._messages_with_memory(
                        agent_id="web_info",
                        system="You are web_info. Return JSON only for WebInfoPack. No extra keys. No markdown.\n\n"
                        + workflow_hint,
                        user=wi_user,
                    )
                    wi, _ = web_info.chat_json(schema=packs.WebInfoPack, messages=wi_msgs, user=wi_user)
                    web_info_ptr = self.artifacts.put_json(wi.model_dump(), suffix=".web_info.json", kind="web_info").to_pointer()
                    self._append_guarded(
                        event=new_event(
                            agent="web_info",
                            type="WEB_INFO_FETCHED",
                            summary="Web info fetched",
                            branch_id=self.branch_id,
                            pointers=[web_info_ptr],
                            meta={"route_level": route_level, "style": resolved_style, "task_id": task_evt.id, "query": wi.query},
                        ),
                        activated_agents=activated_agents,
                    )
                    try:
                        ctx.log_pointers = list(ctx.log_pointers) + [web_info_ptr]
                    except Exception:
                        pass
                except Exception:
                    web_info_ptr = None

            activate_agent("api_confirm", reason="gate:contract")
            if api_confirm is None:
                raise RuntimeError("api_confirm is required for contract/external API changes")
            contract_user = (
                f"Task:\n{task_text}\n\nRequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                f"UseCasePack:\n{usecases.model_dump_json() if usecases is not None else '{}'}\n\nContextPacket:\n{ctx.model_dump_json()}"
            )
            if ctx_excerpts:
                contract_user = f"{contract_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
            if web_info_ptr:
                contract_user = f"{contract_user}\n\nWebInfoPointers:\n- {web_info_ptr}"
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
                    meta={"route_level": route_level, "style": resolved_style, "task_id": task_evt.id},
                ),
                activated_agents=activated_agents,
            )

        if not resume_mode:
            plan_user = (
                f"RequirementPack:\n{req.model_dump_json()}\n\nContextPacket:\n{ctx.model_dump_json()}"
                if req is not None
                else f"Task:\n{task_text}\n\nContextPacket:\n{ctx.model_dump_json()}"
            )
            if ctx_excerpts:
                plan_user = f"{plan_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
            if usecases is not None:
                plan_user = f"{plan_user}\n\nUseCasePack:\n{usecases.model_dump_json()}"
            if intent is not None:
                plan_user = f"{plan_user}\n\nIntentExpansionPack:\n{intent.model_dump_json()}"
            if decisions is not None:
                plan_user = f"{plan_user}\n\nDecisionPack:\n{decisions.model_dump_json()}"
            if contract is not None:
                plan_user = f"{plan_user}\n\nContractPack:\n{contract.model_dump_json()}"
            router_msgs = self._messages_with_memory(
                agent_id="router",
                system=(
                    f"你是调度器 Router：把需求拆成最多 {max_plan_tasks} 个可执行任务。\n"
                    "只输出 JSON（不要 markdown），并严格匹配 Plan schema：{tasks:[{id,title,agent,description}]}。\n"
                    "不要额外 key；不要最外层包一层对象。\n\n"
                    "规划规则：\n"
                    f"- tasks <= {max_plan_tasks}；每个 task 必须可落地、可验收。\n"
                    "- tasks 按顺序执行：先补齐工程骨架/可运行验证，再实现核心功能，最后补文档/收尾。\n"
                    "- task.agent 尽量选择可落地写文件的工种（coder_backend/coder_frontend/integration_engineer）；不要把 pm/qa/reviewer/security 当成 plan task（它们由 gate 触发）。\n"
                    "- 交付导向：至少包含一个任务负责「README/运行方式/最小验证」的交付说明。\n"
                    "- 若需求暗示实时/价格/行情/外部数据：至少包含一个任务负责数据源落地（真实优先，失败回退 mock 并标注 source）。\n\n"
                    f"{workflow_hint}"
                ),
                user=plan_user,
            )
            plan, _plan_meta = router.chat_json(
                schema=packs.Plan,
                messages=router_msgs,
                user=plan_user,
            )
            plan = augment_plan(plan, req=req, task_text=task_text, activated_agents=activated_agents, max_tasks=max_plan_tasks)
            try:
                plan_ptr = self.artifacts.put_json(plan.model_dump(), suffix=".plan.json", kind="plan").to_pointer()
            except Exception:
                plan_ptr = None
            self._append_guarded(
                event=new_event(
                    agent="router",
                    type="PLAN_CREATED",
                    summary=f"Planned {len(plan.tasks)} tasks",
                    branch_id=self.branch_id,
                    pointers=[p for p in [plan_ptr] if p],
                    meta={"route_level": route_level, "style": resolved_style, "task_id": task_evt.id},
                ),
                activated_agents=activated_agents,
            )

        if plan is None:
            plan = packs.Plan(tasks=[])

        available_agents: Set[str] = set(self.config.agents.keys())
        primary_coder_id = self._select_primary_coder(task_text=task_text, risks=risks, activated_agents=available_agents)
        if primary_coder_id == "coder_frontend" and coder_frontend is None:
            primary_coder_id = "coder_backend"
        if primary_coder_id == "integration_engineer" and integrator is None:
            primary_coder_id = "coder_backend"
        activate_agent(primary_coder_id, reason="gate:implement")

        # If the repo appears to be a "from-scratch" scaffold (no build/test config),
        # ensure the plan starts with a bootstrap task so we don't rely on QA failures
        # to discover missing project plumbing.
        try:
            needs_bootstrap = any("工程骨架提示" in str(c) for c in (ctx.constraints or []))
        except Exception:
            needs_bootstrap = False
        if (not resume_mode) and needs_bootstrap:
            try:
                blob = "\n".join([f"{t.title}\n{t.description}" for t in list(plan.tasks or [])]).lower()
            except Exception:
                blob = ""
            has_bootstrap = any(k in blob for k in ["工程骨架", "scaffold", "package.json", "pyproject", "tsconfig", "初始化", "bootstrap"])
            if not has_bootstrap:
                boot = packs.PlanTask(
                    id="t_bootstrap",
                    title="工程骨架",
                    agent=primary_coder_id,
                    description="补齐最小工程骨架（build/lint/test/README 运行说明），确保至少有一条可执行的本地验证命令。",
                )
                plan.tasks = [boot] + list(plan.tasks or [])[:4]

        impl_blueprint: Optional[packs.ImplementationBlueprint] = None
        impl_blueprint_ptr: Optional[str] = None
        lead_fix_blueprint_keys: set[str] = set()
        lead_consult_cache: dict[str, tuple[str, list[str]]] = {}
        lead_fix_agents = {"coder_backend", "coder_frontend", "integration_engineer"}
        lead_consult_advisors = {"architect", "env_engineer", "api_confirm", "ops_engineer"}

        def _sanitize_blueprint(bp: packs.ImplementationBlueprint) -> packs.ImplementationBlueprint:
            def clean(pats: List[str], *, limit: int) -> List[str]:
                out: list[str] = []
                seen: set[str] = set()
                for raw in list(pats or [])[: max(0, limit * 3)]:
                    s = _normalize_scope_pattern(str(raw or ""))
                    if not s:
                        continue
                    # Never allow internal system dirs, and avoid unsafe paths.
                    if s.startswith(".vibe/") or s.startswith(".git/"):
                        continue
                    if ":" in s or s.startswith("\\\\") or s.startswith("//") or "/../" in f"/{s}/":
                        continue
                    if s not in seen:
                        seen.add(s)
                        out.append(s)
                    if len(out) >= limit:
                        break
                return out

            bp.global_allowed_write_globs = clean(list(bp.global_allowed_write_globs or []), limit=48) or ["**"]
            bp.global_denied_write_globs = clean(list(bp.global_denied_write_globs or []), limit=48)
            bp.fix_allowed_write_globs = clean(list(bp.fix_allowed_write_globs or []), limit=48)
            bp.fix_denied_write_globs = clean(list(bp.fix_denied_write_globs or []), limit=48)
            try:
                scopes = list(bp.task_scopes or [])
            except Exception:
                scopes = []
            norm_scopes: list[packs.ImplementationBlueprintTaskScope] = []
            for s in scopes[:64]:
                try:
                    sid = str(getattr(s, "task_id", "") or "").strip()
                except Exception:
                    sid = ""
                if not sid:
                    continue
                allow = clean(list(getattr(s, "allowed_write_globs", []) or []), limit=24)
                deny = clean(list(getattr(s, "denied_write_globs", []) or []), limit=24)
                notes = str(getattr(s, "notes", "") or "").strip()
                norm_scopes.append(
                    packs.ImplementationBlueprintTaskScope(
                        task_id=sid,
                        allowed_write_globs=allow,
                        denied_write_globs=deny,
                        notes=notes,
                    )
                )
            bp.task_scopes = norm_scopes
            rec_fix = str(getattr(bp, "recommended_fix_agent", "") or "").strip()
            bp.recommended_fix_agent = rec_fix if rec_fix in lead_fix_agents else ""
            consults: list[str] = []
            seen_consults: set[str] = set()
            for raw in list(getattr(bp, "consult_agents", []) or [])[:16]:
                aid = str(raw or "").strip()
                if not aid or aid not in lead_consult_advisors or aid in seen_consults:
                    continue
                seen_consults.add(aid)
                consults.append(aid)
            bp.consult_agents = consults
            bp.escalation_reason = str(getattr(bp, "escalation_reason", "") or "").strip()[:240]
            bp.invariants = [str(x).strip()[:240] for x in list(bp.invariants or []) if str(x).strip()][:16]
            bp.verification = [str(x).strip()[:240] for x in list(bp.verification or []) if str(x).strip()][:12]
            bp.pointers = [str(x).strip() for x in list(bp.pointers or []) if str(x).strip()][:24]
            return bp

        if resume_impl_blueprint_candidate is not None:
            try:
                impl_blueprint = _sanitize_blueprint(resume_impl_blueprint_candidate)
                impl_blueprint_ptr = resume_impl_blueprint_ptr
            except Exception:
                impl_blueprint = None
                impl_blueprint_ptr = None

        def _scope_for_plan_task(task: packs.PlanTask) -> tuple[list[str], list[str]]:
            if impl_blueprint is None:
                return [], []
            tid = str(getattr(task, "id", "") or "").strip()
            allow: list[str] = []
            deny: list[str] = []
            for s in list(impl_blueprint.task_scopes or [])[:96]:
                if str(getattr(s, "task_id", "") or "").strip() != tid:
                    continue
                allow = list(getattr(s, "allowed_write_globs", []) or [])
                deny = list(getattr(s, "denied_write_globs", []) or [])
                break
            if not allow:
                allow = list(impl_blueprint.global_allowed_write_globs or [])
            deny = list(impl_blueprint.global_denied_write_globs or []) + list(deny or [])
            return allow, deny

        def _scope_for_fix_loop() -> tuple[list[str], list[str]]:
            if impl_blueprint is None:
                return [], []
            allow = list(impl_blueprint.fix_allowed_write_globs or [])
            if not allow:
                allow = list(impl_blueprint.global_allowed_write_globs or [])
            deny = list(impl_blueprint.global_denied_write_globs or []) + list(impl_blueprint.fix_denied_write_globs or [])
            return allow, deny

        def _preferred_fix_agent(current_fix_agent: str) -> str:
            preferred = str(getattr(impl_blueprint, "recommended_fix_agent", "") or "").strip() if impl_blueprint is not None else ""
            if preferred in lead_fix_agents and preferred in self.config.agents:
                return preferred
            return current_fix_agent

        def maybe_build_implementation_blueprint(*, reason: str) -> None:
            nonlocal impl_blueprint, impl_blueprint_ptr
            if resume_mode and not resume_replan_mode:
                return
            if resume_replan_mode and impl_blueprint is not None:
                return
            cfg = self.config.agents.get("implementation_lead")
            if cfg is None or not bool(getattr(cfg, "enabled", True)):
                return
            if route_level not in {"L2", "L3", "L4"}:
                return
            try:
                can_call = mock_mode or self._api_key_available_for_agent("implementation_lead")
            except Exception:
                can_call = False
            if not can_call or implementation_lead is None:
                return

            try:
                activate_agent("implementation_lead", reason=reason)
            except Exception:
                pass

            bp_system = (
                "你是 implementation_lead（技术主管/代码一致性负责人）。目标：把架构/计划翻译成可落地的文件级实现蓝图，"
                "减少 coder 各自为政导致的漂移/屎山化。\n"
                "只输出符合 ImplementationBlueprint schema 的 JSON（不要 markdown，不要包裹对象）。\n\n"
                "你必须给出：\n"
                "- summary：一句话\n"
                "- global_allowed_write_globs：全局允许改动的文件/路径 glob（repo-root 相对路径，使用 / 分隔；尽量收敛但要够用）\n"
                "- task_scopes：为每个 PlanTask(task_id) 给更窄的 allowed_write_globs（可为空，表示沿用 global）\n"
                "- fix_allowed_write_globs：fix-loop 允许改动的范围（可为空，表示沿用 global）\n"
                "- invariants：跨任务必须保持一致的约束（端口/env 变量/目录命名/公共类型等）\n"
                "- verification：最后如何验证（命令层面）\n\n"
                "硬规则：\n"
                "- 绝对禁止 `.vibe/**`、`.git/**`。\n"
                "- 绝不要臆造“已有文件/脚本/页面”；如需新增文件，必须把路径纳入 allowed globs。\n"
                "- 默认不要修改 node_modules；只有在 Windows shim/binary workaround 等必要时，才允许最小范围的 copy/writes。\n\n"
                f"{workflow_hint}"
            )
            bp_user = (
                f"Task:\n{task_text}\n\n"
                f"RouteLevel: {route_level}\nStyle: {resolved_style}\n\n"
                f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                f"UseCasePack:\n{usecases.model_dump_json() if usecases is not None else '{}'}\n\n"
                f"DecisionPack:\n{decisions.model_dump_json() if decisions is not None else '{}'}\n\n"
                f"ContractPack:\n{contract.model_dump_json() if contract is not None else '{}'}\n\n"
                f"FullPlan:\n{plan.model_dump_json()}\n\n"
                f"ContextPacket:\n{ctx.model_dump_json()}"
            )
            if ctx_excerpts:
                bp_user = f"{bp_user}\n\nRepoExcerpts:\n{ctx_excerpts}"

            try:
                bp_msgs = self._messages_with_memory(agent_id="implementation_lead", system=bp_system, user=bp_user)
                bp, _ = implementation_lead.chat_json(schema=packs.ImplementationBlueprint, messages=bp_msgs, user=bp_user)
                bp = _sanitize_blueprint(bp)
                ptr = self.artifacts.put_json(bp.model_dump(), suffix=".impl_blueprint.json", kind="impl_blueprint").to_pointer()
                impl_blueprint = bp
                impl_blueprint_ptr = ptr
                self._append_guarded(
                    event=new_event(
                        agent="router",
                        type="LEAD_BLUEPRINT_BUILT",
                        summary="Implementation blueprint built",
                        branch_id=self.branch_id,
                        pointers=[ptr],
                        meta={"route_level": route_level, "style": resolved_style, "task_id": task_evt.id, "tasks": len(list(plan.tasks or []))},
                    ),
                    activated_agents=activated_agents,
                )
                try:
                    ctx.log_pointers = list(ctx.log_pointers) + [ptr]
                except Exception:
                    pass
            except Exception:
                impl_blueprint = None
                impl_blueprint_ptr = None

        def maybe_build_fix_blueprint(
            *,
            reason: str,
            blocker_source: str,
            blocker_text: str,
            extracted: list[str],
            report: packs.TestReport,
        ) -> None:
            """
            Escalation path: when fix-loop stagnates, consult implementation_lead to
            provide a tighter file-level scope and invariants for convergence.
            """
            nonlocal impl_blueprint, impl_blueprint_ptr
            cfg = self.config.agents.get("implementation_lead")
            if cfg is None or not bool(getattr(cfg, "enabled", True)):
                return
            if route_level == "L0":
                return
            try:
                can_call = mock_mode or self._api_key_available_for_agent("implementation_lead")
            except Exception:
                can_call = False
            if not can_call or implementation_lead is None:
                return

            try:
                activate_agent("implementation_lead", reason=reason)
            except Exception:
                pass

            bp_system = (
                "你是 implementation_lead（技术主管/代码一致性负责人）。当前工作流卡在 fix-loop，需要你提供“可收敛”的文件级修复范围与约束。\n"
                "只输出符合 ImplementationBlueprint schema 的 JSON（不要 markdown，不要包裹对象）。\n"
                "重点要求：\n"
                "- fix_allowed_write_globs：只允许修复当前 blocker 所需的最小范围（可为空表示不限制）。\n"
                "- fix_denied_write_globs：明确禁止改动的范围（可为空）。\n"
                "- recommended_fix_agent：如果当前问题应由别的代码工种主修，填 coder_backend / coder_frontend / integration_engineer 之一。\n"
                "- consult_agents：如果需要额外会诊，填 architect / env_engineer / api_confirm / ops_engineer 的子集。\n"
                "- escalation_reason：说明为什么要改派/会诊（简短即可）。\n"
                "- invariants：跨文件必须保持一致的关键约束。\n"
                "- verification：优先给出最小验证命令集合（围绕失败命令）。\n"
                "可选：task_scopes 可以留空；global_allowed_write_globs 仅在需要时给出。\n\n"
                "硬规则：\n"
                "- 绝对禁止 `.vibe/**`、`.git/**`。\n"
                "- 不要凭空编造文件存在；如果你允许新增文件，请在 allow globs 里覆盖它。\n"
                "- 默认不要修改 node_modules；只有在 Windows shim/binary workaround 等必要时，才允许最小范围 copy/writes。\n\n"
                f"{workflow_hint}"
            )
            bp_user = (
                f"Task:\n{task_text}\n\n"
                f"BlockerSource: {blocker_source}\n"
                f"Blocker:\n{blocker_text}\n\n"
                + (
                    "ExtractedErrors:\n" + "\n".join([f"- {x}" for x in list(extracted or [])[:20]]) + "\n\n"
                    if extracted
                    else ""
                )
                + f"TestReport:\n{report.model_dump_json()}\n\n"
                + f"ContextPacket:\n{ctx.model_dump_json()}\n"
            )
            if ctx_excerpts:
                bp_user = f"{bp_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
            if impl_blueprint is not None:
                bp_user = f"{bp_user}\n\nExistingBlueprint:\n{impl_blueprint.model_dump_json()}"
            if fix_history:
                bp_user = f"{bp_user}\n\nRecentFixHistory:\n" + "\n".join(fix_history[-6:])

            try:
                bp_msgs = self._messages_with_memory(agent_id="implementation_lead", system=bp_system, user=bp_user)
                bp, _ = implementation_lead.chat_json(schema=packs.ImplementationBlueprint, messages=bp_msgs, user=bp_user)
                bp = _sanitize_blueprint(bp)
                ptr = self.artifacts.put_json(bp.model_dump(), suffix=".impl_blueprint.fix.json", kind="impl_blueprint").to_pointer()
                impl_blueprint = bp
                impl_blueprint_ptr = ptr
                self._append_guarded(
                    event=new_event(
                        agent="router",
                        type="LEAD_BLUEPRINT_BUILT",
                        summary="Implementation blueprint updated for fix-loop",
                        branch_id=self.branch_id,
                        pointers=[ptr],
                        meta={"route_level": route_level, "style": resolved_style, "task_id": task_evt.id, "phase": "fix_loop"},
                    ),
                    activated_agents=activated_agents,
                )
            except Exception:
                pass

        def maybe_collect_lead_consults(
            *,
            loop: int,
            blocker_source: str,
            blocker_text: str,
            extracted: list[str],
            report: packs.TestReport,
            failure_fingerprint: str,
        ) -> tuple[str, list[str]]:
            if impl_blueprint is None:
                return "", []
            advisors = [str(x).strip() for x in list(getattr(impl_blueprint, "consult_agents", []) or []) if str(x).strip()]
            if not advisors:
                return "", []

            cache_key = "|".join(
                [
                    blocker_source,
                    failure_fingerprint or blocker_text[:120],
                    ",".join(advisors[:6]),
                ]
            )
            if cache_key in lead_consult_cache:
                return lead_consult_cache[cache_key]

            advice_parts: list[str] = []
            ptrs: list[str] = []
            for aid in advisors[:3]:
                cfg = self.config.agents.get(aid)
                if cfg is None or not bool(getattr(cfg, "enabled", True)):
                    continue
                try:
                    can_call = mock_mode or self._api_key_available_for_agent(aid)
                except Exception:
                    can_call = False
                if not can_call:
                    continue
                try:
                    activate_agent(aid, reason="gate:lead_consult")
                except Exception:
                    pass
                try:
                    advisor = self._agent(aid)
                    advice_system = (
                        f"You are {aid}. The implementation_lead is escalating a blocked workflow and needs advisory input.\n"
                        "Return JSON only matching ChatReply schema: {reply: string, suggested_actions: string[], pointers: string[]}.\n"
                        "Rules:\n"
                        "- Base your advice only on the supplied blocker/test evidence/context.\n"
                        "- Keep it short and actionable.\n"
                        "- Do not claim files/commands exist unless the evidence shows it.\n\n"
                        f"{workflow_hint}"
                    )
                    advice_user = (
                        f"Task:\n{task_text}\n\n"
                        f"RouteLevel: {route_level}\n"
                        f"BlockerSource: {blocker_source}\n"
                        f"FailureFingerprint: {failure_fingerprint}\n"
                        f"ImplementationLeadReason: {str(getattr(impl_blueprint, 'escalation_reason', '') or '').strip()}\n\n"
                        f"Blocker:\n{blocker_text}\n\n"
                        f"TestReport:\n{report.model_dump_json()}\n\n"
                        f"ContextPacket:\n{ctx.model_dump_json()}"
                    )
                    if extracted:
                        advice_user = f"{advice_user}\n\nExtractedErrors:\n" + "\n".join([f"- {x}" for x in extracted[:20]])
                    if ctx_excerpts:
                        advice_user = f"{advice_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
                    advice_msgs = self._messages_with_memory(agent_id=aid, system=advice_system, user=advice_user)
                    advice, _ = advisor.chat_json(schema=packs.ChatReply, messages=advice_msgs, user=advice_user)
                    ptr = self.artifacts.put_json(
                        {"agent": aid, "advice": advice.model_dump()},
                        suffix=f".{aid}.advice.json",
                        kind="lead_consult",
                    ).to_pointer()
                    ptrs.append(ptr)
                    advice_parts.append(f"{aid}: {advice.model_dump_json()}")
                    self._append_guarded(
                        event=new_event(
                            agent="router",
                            type="STATE_TRANSITION",
                            summary=f"Fix-loop {loop}: consulted {aid} via implementation_lead",
                            branch_id=self.branch_id,
                            pointers=[ptr],
                            meta={
                                "phase": "fix_loop",
                                "loop": loop,
                                "action": "lead_consult",
                                "advisor": aid,
                                "route_level": route_level,
                                "style": resolved_style,
                            },
                        ),
                        activated_agents=activated_agents,
                    )
                except Exception:
                    continue

            text = ""
            if advice_parts:
                text = "LeadAdvisorNotes:\n" + "\n\n".join(advice_parts)
            lead_consult_cache[cache_key] = (text, ptrs)
            return text, ptrs

        maybe_build_implementation_blueprint(reason="gate:blueprint")

        def coder_actor(agent_id: str):
            if agent_id == "coder_frontend" and coder_frontend is not None:
                return coder_frontend, "Frontend Coder (React/TypeScript)"
            if agent_id == "integration_engineer" and integrator is not None:
                return integrator, "Integration Engineer (align frontend/backend/contracts)"
            return coder_backend, "Backend Coder"

        primary_coder, coder_role = coder_actor(primary_coder_id)

        def maybe_prepare_replan(
            *,
            loop: int,
            blocker_source: str,
            blocker_text: str,
            extracted: list[str],
            failure_fingerprint: str,
            report: packs.TestReport,
            review: Optional[packs.ReviewReport],
            security_report: Optional[packs.RiskRegister],
            compliance_report: Optional[packs.ComplianceReport],
            perf_report: Optional[packs.PerfReport],
            incident: Optional[packs.IncidentPack],
            incident_ptr: Optional[str],
            fix_plan: Optional[packs.FixPlanPack],
            fix_plan_ptr: Optional[str],
            stagnating_hard: bool,
        ) -> tuple[bool, str]:
            nonlocal decisions, decisions_ptr, contract, contract_ptr, plan, plan_ptr, impl_blueprint, impl_blueprint_ptr
            if route_level not in {"L2", "L3", "L4"}:
                return False, ""
            if resume_replan_mode:
                return False, ""

            consults = {
                str(x).strip()
                for x in list(getattr(impl_blueprint, "consult_agents", []) or [])
                if str(x).strip()
            }
            escalation_reason = str(getattr(impl_blueprint, "escalation_reason", "") or "").strip()
            haystack = "\n".join(
                [
                    blocker_source,
                    blocker_text,
                    escalation_reason,
                    "\n".join(list(extracted or [])[:12]),
                    "\n".join(list(getattr(report, "blockers", []) or [])[:8]),
                ]
            ).lower()

            trigger_reasons: list[str] = []
            if blocker_source in {"review", "security", "compliance", "performance"} and loop >= 2:
                trigger_reasons.append(f"{blocker_source}_gate_stuck")
            if blocker_source == "tests" and stagnating_hard:
                if consults.intersection({"architect", "api_confirm"}):
                    trigger_reasons.append("lead_requested_design_consult")
                elif any(keyword in haystack for keyword in REPLAN_HINT_KEYWORDS):
                    trigger_reasons.append("design_level_failure_signature")

            if not trigger_reasons:
                return False, ""
            if architect is None:
                return False, ""

            try:
                activate_agent("architect", reason="gate:replan")
            except Exception:
                pass

            replan_summary = ", ".join(trigger_reasons[:4])
            arch_user = (
                f"Task:\n{task_text}\n\n"
                f"RouteLevel: {route_level}\n"
                f"ReplanReasons:\n- " + "\n- ".join(trigger_reasons[:6]) + "\n\n"
                f"BlockerSource: {blocker_source}\n"
                f"FailureFingerprint: {failure_fingerprint}\n\n"
                f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                f"UseCasePack:\n{usecases.model_dump_json() if usecases is not None else '{}'}\n\n"
                f"PreviousDecisionPack:\n{decisions.model_dump_json() if decisions is not None else '{}'}\n\n"
                f"CurrentPlan:\n{plan.model_dump_json() if plan is not None else '{}'}\n\n"
                f"ContractPack:\n{contract.model_dump_json() if contract is not None else '{}'}\n\n"
                f"TestReport:\n{report.model_dump_json()}\n\n"
                f"ReviewReport:\n{review.model_dump_json() if review is not None else '{}'}\n\n"
                f"SecurityReport:\n{security_report.model_dump_json() if security_report is not None else '{}'}\n\n"
                f"ComplianceReport:\n{compliance_report.model_dump_json() if compliance_report is not None else '{}'}\n\n"
                f"PerfReport:\n{perf_report.model_dump_json() if perf_report is not None else '{}'}\n\n"
                f"ContextPacket:\n{ctx.model_dump_json()}\n\n"
                f"Blocker:\n{blocker_text}"
            )
            if incident is not None:
                arch_user = f"{arch_user}\n\nIncidentPack:\n{incident.model_dump_json()}"
            if fix_plan is not None:
                arch_user = f"{arch_user}\n\nFixPlan:\n{fix_plan.model_dump_json()}"
            if fix_history:
                arch_user = f"{arch_user}\n\nRecentFixHistory:\n" + "\n".join(fix_history[-6:])
            if ctx_excerpts:
                arch_user = f"{arch_user}\n\nRepoExcerpts:\n{ctx_excerpts}"

            try:
                arch_msgs = self._messages_with_memory(
                    agent_id="architect",
                    system=(
                        "你是架构师（architect）。当前方案已被 blocker 证明存在设计/契约/模块边界层面的缺口，需要做一次“窄重规划”。\n"
                        "只输出 DecisionPack JSON；不要 markdown；不要额外 key。\n"
                        "要求：\n"
                        "- 不要推翻整个方案，只修正导致 blocker 的共享约定、模块边界、目录/命令/env 契约。\n"
                        "- shared_context 必须继续包含 repo_layout / commands / env_vars。\n"
                        "- decision 必须面向“下一轮实现能收敛”，而不是泛泛而谈。\n\n"
                        f"{workflow_hint}"
                    ),
                    user=arch_user,
                )
                replanned_decisions, _ = architect.chat_json(schema=packs.DecisionPack, messages=arch_msgs, user=arch_user)
                decisions = replanned_decisions
                decisions_ptr = self.artifacts.put_json(
                    decisions.model_dump(),
                    suffix=".adr.replan.json",
                    kind="adr",
                ).to_pointer()
                self._append_guarded(
                    event=new_event(
                        agent="architect",
                        type="ARCH_UPDATED",
                        summary="Architecture replanned after blockers",
                        branch_id=self.branch_id,
                        pointers=[p for p in [decisions_ptr, incident_ptr, fix_plan_ptr] if p],
                        meta={
                            "route_level": route_level,
                            "style": resolved_style,
                            "phase": "replan",
                            "reasons": trigger_reasons[:6],
                            "failure_fingerprint": failure_fingerprint,
                        },
                    ),
                    activated_agents=activated_agents,
                )
            except Exception:
                return False, ""

            refresh_contract = route_level in {"L2", "L3", "L4"} and (
                contract is not None
                or risks.contract_change
                or risks.touches_external_api
                or "api_confirm" in consults
            )
            if refresh_contract and api_confirm is not None:
                try:
                    activate_agent("api_confirm", reason="gate:replan_contract")
                    contract_user = (
                        f"Task:\n{task_text}\n\n"
                        f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                        f"UseCasePack:\n{usecases.model_dump_json() if usecases is not None else '{}'}\n\n"
                        f"DecisionPack:\n{decisions.model_dump_json() if decisions is not None else '{}'}\n\n"
                        f"PreviousContractPack:\n{contract.model_dump_json() if contract is not None else '{}'}\n\n"
                        f"BlockerSource: {blocker_source}\n"
                        f"FailureFingerprint: {failure_fingerprint}\n\n"
                        f"Blocker:\n{blocker_text}\n\n"
                        f"ContextPacket:\n{ctx.model_dump_json()}"
                    )
                    if ctx_excerpts:
                        contract_user = f"{contract_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
                    contract_msgs = self._messages_with_memory(
                        agent_id="api_confirm",
                        system=(
                            "你是 API/Contract confirmer。当前 blocker 需要你刷新 ContractPack，使下一轮实现/验证不再基于失效契约。\n"
                            "只输出 ContractPack JSON；不要 markdown；不要额外 key。\n\n"
                            f"{workflow_hint}"
                        ),
                        user=contract_user,
                    )
                    contract, _ = api_confirm.chat_json(schema=packs.ContractPack, messages=contract_msgs, user=contract_user)
                    contract_ptr = self.artifacts.put_json(
                        contract.model_dump(),
                        suffix=".contract.replan.json",
                        kind="contract",
                    ).to_pointer()
                    self._append_guarded(
                        event=new_event(
                            agent="api_confirm",
                            type="CONTRACT_CONFIRMED",
                            summary="Contract refreshed during replan",
                            branch_id=self.branch_id,
                            pointers=[contract_ptr],
                            meta={
                                "route_level": route_level,
                                "style": resolved_style,
                                "phase": "replan",
                                "failure_fingerprint": failure_fingerprint,
                            },
                        ),
                        activated_agents=activated_agents,
                    )
                except Exception:
                    pass

            replan_user = (
                f"Task:\n{task_text}\n\n"
                f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                f"IntentExpansionPack:\n{intent.model_dump_json() if intent is not None else '{}'}\n\n"
                f"UseCasePack:\n{usecases.model_dump_json() if usecases is not None else '{}'}\n\n"
                f"DecisionPack:\n{decisions.model_dump_json() if decisions is not None else '{}'}\n\n"
                f"ContractPack:\n{contract.model_dump_json() if contract is not None else '{}'}\n\n"
                f"PreviousPlan:\n{plan.model_dump_json() if plan is not None else '{}'}\n\n"
                f"BlockerSource: {blocker_source}\n"
                f"FailureFingerprint: {failure_fingerprint}\n"
                f"ReplanReasons:\n- " + "\n- ".join(trigger_reasons[:6]) + "\n\n"
                f"Blocker:\n{blocker_text}\n\n"
                f"ContextPacket:\n{ctx.model_dump_json()}"
            )
            if fix_history:
                replan_user = f"{replan_user}\n\nRecentFixHistory:\n" + "\n".join(fix_history[-6:])
            if ctx_excerpts:
                replan_user = f"{replan_user}\n\nRepoExcerpts:\n{ctx_excerpts}"

            try:
                replan_msgs = self._messages_with_memory(
                    agent_id="router",
                    system=(
                        f"你是调度器 Router。当前计划在 fix-loop 中被证据证明不收敛，需要输出一版新的执行计划（最多 {max_plan_tasks} 个任务）。\n"
                        "只输出 Plan JSON；不要 markdown；不要额外 key。\n"
                        "规则：\n"
                        f"- tasks <= {max_plan_tasks}。\n"
                        "- 只重排/补足必要任务，不要把整个计划推翻重写。\n"
                        "- 先补真正缺失的工程骨架/共享类型/命令/入口，再做业务实现，最后再进 QA。\n"
                        "- 如果 blocker 暗示跨模块对齐问题，优先给 integration_engineer 明确任务。\n\n"
                        f"{workflow_hint}"
                    ),
                    user=replan_user,
                )
                replanned_plan, _ = router.chat_json(schema=packs.Plan, messages=replan_msgs, user=replan_user)
                plan = augment_plan(
                    replanned_plan,
                    req=req,
                    task_text=task_text,
                    activated_agents=activated_agents,
                    max_tasks=max_plan_tasks,
                )
                try:
                    needs_bootstrap = any("工程骨架提示" in str(c) for c in (ctx.constraints or []))
                except Exception:
                    needs_bootstrap = False
                if needs_bootstrap:
                    try:
                        blob = "\n".join([f"{t.title}\n{t.description}" for t in list(plan.tasks or [])]).lower()
                    except Exception:
                        blob = ""
                    has_bootstrap = any(
                        k in blob for k in ["工程骨架", "scaffold", "package.json", "pyproject", "tsconfig", "初始化", "bootstrap"]
                    )
                    if not has_bootstrap:
                        plan.tasks = [
                            packs.PlanTask(
                                id="t_bootstrap",
                                title="工程骨架",
                                agent=primary_coder_id,
                                description="补齐最小工程骨架（build/lint/test/README 运行说明），确保至少有一条可执行的本地验证命令。",
                            )
                        ] + list(plan.tasks or [])[:4]
                plan_ptr = self.artifacts.put_json(plan.model_dump(), suffix=".plan.replan.json", kind="plan").to_pointer()
                self._append_guarded(
                    event=new_event(
                        agent="router",
                        type="PLAN_CREATED",
                        summary=f"Replanned {len(plan.tasks)} tasks after blockers",
                        branch_id=self.branch_id,
                        pointers=[p for p in [plan_ptr, decisions_ptr, contract_ptr] if p],
                        meta={
                            "route_level": route_level,
                            "style": resolved_style,
                            "phase": "replan",
                            "task_id": task_evt.id,
                            "reasons": trigger_reasons[:6],
                            "failure_fingerprint": failure_fingerprint,
                        },
                    ),
                    activated_agents=activated_agents,
                )
            except Exception:
                return False, ""

            try:
                maybe_build_implementation_blueprint(reason="gate:blueprint_replan")
            except Exception:
                pass
            return True, replan_summary

        def coder_system(*, role: str, task: packs.PlanTask) -> str:
            scope_lines: list[str] = []
            if impl_blueprint is not None:
                try:
                    allow, deny = _scope_for_plan_task(task)
                except Exception:
                    allow, deny = [], []
                allow = [str(x).strip() for x in list(allow or []) if str(x).strip()][:12]
                deny = [str(x).strip() for x in list(deny or []) if str(x).strip()][:12]
                inv = [str(x).strip() for x in list(impl_blueprint.invariants or []) if str(x).strip()][:8]
                ver = [str(x).strip() for x in list(impl_blueprint.verification or []) if str(x).strip()][:6]
                if allow:
                    scope_lines.append("Allowed write paths/globs (MUST stay within):\n" + "\n".join([f"- {p}" for p in allow]))
                if deny:
                    scope_lines.append("Denied write paths/globs:\n" + "\n".join([f"- {p}" for p in deny]))
                if inv:
                    scope_lines.append("Invariants (keep consistent across tasks):\n" + "\n".join([f"- {p}" for p in inv]))
                if ver:
                    scope_lines.append("End-state verification targets:\n" + "\n".join([f"- {p}" for p in ver]))
            blueprint_hint = ""
            if scope_lines:
                blueprint_hint = "\n\nImplementationLeadBlueprint:\n" + "\n\n".join(scope_lines) + "\n"
            return (
                f"You are {role}. Return JSON only for CodeChange with fields: "
                "kind ('commit'|'patch'|'noop'), summary, writes? (list[{path,content}]), copies? (list[{src,dst}]), commit_hash?, patch_pointer?, files_changed[], blockers[]. "
                "Prefer 'writes' for file changes (especially when starting from an empty repo). "
                "Each writes item must include the full file content. No extra keys. No markdown.\n\n"
                "Focus:\n"
                f"- Implement ONLY this PlanTask ({task.id}): {task.title}\n"
                "- Do not implement other plan tasks yet; keep changes minimal and coherent.\n\n"
                "Hard rules:\n"
                "- Never write under `.vibe/` or `.git/` (those are internal system dirs).\n"
                "- Never copy under `.vibe/` or `.git/`.\n"
                "- Use only repo-root relative paths (no absolute paths / drive letters).\n"
                "- Do not introduce new modules/folders unless you ALSO create them in writes.\n"
                "- Do not import new npm packages unless you ALSO add them to the correct `package.json` (dependencies/devDependencies) in writes.\n"
                "- Do not rely on globally installed CLIs (e.g. hugo/rails/nest/next) unless the ToolingProbe indicates they are present on PATH; prefer writing scaffolding files directly.\n"
                "- Avoid `npx`-based scaffolding (network-dependent). If you must, document it explicitly in README and provide an offline-friendly fallback.\n"
                "- Do not do large refactors; prefer the smallest coherent change set.\n"
                "- If you change exports/imports, ensure all references stay consistent.\n"
                "- For TypeScript repos, aim to make `npm run build` pass in affected node project(s).\n"
                "- If you add a Vite app, include `index.html` at that app root.\n"
                "- If you add/enable ESLint, include an ESLint config and required TS parser/plugins.\n"
                "- NPM scripts must be Windows-compatible: avoid single quotes around globs; prefer double quotes.\n"
                "- Windows/Node: do NOT hardcode or spawn `node_modules/.bin/<tool>.exe` (may be a 0-byte shim). Prefer `npm run <script>` or `<tool>.cmd` on Windows.\n"
                "- For env vars in scripts (e.g. NODE_ENV=production), prefer `cross-env` for cross-platform.\n"
                "\n"
                "- Delivery-first: if the task implies \"real-time\"/\"price\"/\"live data\", implement a configurable real data source when feasible; "
                "otherwise fall back to mock BUT label it clearly (e.g. `source=mock`) and document how to switch to real data in README.\n"
                "- Never claim \"real\" data if it's mock; keep the UI/API honest.\n"
                "\n\n"
                f"{blueprint_hint}{workflow_hint}"
            )

        def task_user_text(*, task: packs.PlanTask) -> str:
            base = (
                f"Task:\n{task_text}\n\n"
                f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                f"IntentExpansionPack:\n{intent.model_dump_json() if intent is not None else '{}'}\n\n"
                f"DecisionPack:\n{decisions.model_dump_json() if decisions is not None else '{}'}\n\n"
                f"ContractPack:\n{contract.model_dump_json() if contract is not None else '{}'}\n\n"
                f"PlanTask:\n{task.model_dump_json()}\n\n"
                f"FullPlan:\n{plan.model_dump_json()}\n\n"
                f"ContextPacket:\n{ctx.model_dump_json()}"
            )
            if usecases is not None:
                base = f"{base}\n\nUseCasePack:\n{usecases.model_dump_json()}"
            if ctx_excerpts:
                base = f"{base}\n\nRepoExcerpts:\n{ctx_excerpts}"
            return base

        # Execute plan tasks sequentially. This avoids the "QA drives progress" anti-pattern:
        # we first complete the planned work, then validate.
        #
        # Resume from ordinary non-green checkpoints MUST NOT re-run PLAN/IMPLEMENT.
        # Replan checkpoints are different: they carry a refreshed plan and should
        # continue from IMPLEMENT on the next run.
        plan_changes: list[packs.CodeChange] = []
        all_write_pointers: list[str] = []

        if not resume_skip_implement:
            code_agents = {"coder_backend", "coder_frontend", "integration_engineer"}
            for t in list(plan.tasks or [])[:max_implement_tasks]:
                agent_id = (t.agent or "").strip() or primary_coder_id
                if agent_id not in code_agents:
                    agent_id = primary_coder_id
                if agent_id == "coder_frontend" and coder_frontend is None:
                    agent_id = "coder_backend"
                if agent_id == "integration_engineer" and integrator is None:
                    agent_id = "coder_backend"

                activate_agent(agent_id, reason=f"plan_task:{t.id}")
                actor, role = coder_actor(agent_id)
                self._append_guarded(
                    event=new_event(
                        agent="router",
                        type="STATE_TRANSITION",
                        summary=f"Implement: {t.id} -> {agent_id}",
                        branch_id=self.branch_id,
                        pointers=[],
                        meta={
                            "phase": "implement",
                            "plan_task_id": t.id,
                            "plan_task_title": t.title,
                            "agent": agent_id,
                            "route_level": route_level,
                            "style": resolved_style,
                        },
                    ),
                    activated_agents=activated_agents,
                )

                user_text = task_user_text(task=t)
                msgs = self._messages_with_memory(
                    agent_id=agent_id,
                    system=coder_system(role=role, task=t),
                    user=user_text,
                )
                task_change, _ = actor.chat_json(schema=packs.CodeChange, messages=msgs, user=user_text)
                task_allow, task_deny = _scope_for_plan_task(t) if impl_blueprint is not None else ([], [])
                task_change, write_ptrs = self._materialize_code_change_with_repair(
                    change=task_change,
                    actor_agent_id=agent_id,
                    actor=actor,
                    actor_role=role,
                    workflow_hint=workflow_hint,
                    activated_agents=activated_agents,
                    activate_agent=activate_agent,
                    route_level=route_level,
                    style=resolved_style,
                    write_allowlist=task_allow,
                    write_denylist=task_deny,
                )

                # Keep prompts downstream small: we only need patch evidence + file list.
                task_change = task_change.model_copy(update={"writes": []})

                meta = {"files_changed": task_change.files_changed, "route_level": route_level, "style": resolved_style, "plan_task_id": t.id}
                if task_change.blockers:
                    meta["blockers"] = list(task_change.blockers)[:8]
                self._append_guarded(
                    event=new_event(
                        agent=agent_id,
                        type="PATCH_WRITTEN" if task_change.kind == "patch" else "CODE_COMMIT",
                        summary=f"{t.title}: {task_change.summary}",
                        branch_id=self.branch_id,
                        pointers=[p for p in [task_change.patch_pointer, task_change.commit_hash] if p] + write_ptrs,
                        meta=meta,
                    ),
                    activated_agents=activated_agents,
                )
                plan_changes.append(task_change)
                all_write_pointers.extend(write_ptrs)

                # Keep manifests/index fresh so subsequent tasks can ground in new repo state.
                try:
                    self.toolbox.scan_repo(agent_id="router", reason=f"plan_task:{t.id}")
                except PolicyDeniedError:
                    pass
                except Exception:
                    pass

                # Refresh ContextPacket excerpts so later tasks and gates see updated README/manifests.
                try:
                    ctx, ctx_excerpts = self._build_context_packet(task_evt=task_evt)
                except Exception:
                    pass

        # Aggregate evidence for downstream gates (review/security) without embedding full file contents.
        files_changed: list[str] = []
        seen_files: set[str] = set()
        for c in plan_changes:
            for p in list(c.files_changed or [])[:200]:
                s = str(p).strip()
                if not s or s in seen_files:
                    continue
                seen_files.add(s)
                files_changed.append(s)

        summary_parts = [str(c.summary or "").strip() for c in plan_changes if str(c.summary or "").strip()]
        summary = "；".join(summary_parts)[:240] if summary_parts else (req.summary if req is not None else task_text.strip().splitlines()[0][:120])
        if resume_mode and not summary_parts and resume_from:
            summary = f"resume: continue from {resume_from}"
        change = packs.CodeChange(kind=("patch" if any(c.kind == "patch" for c in plan_changes) else "noop"), summary=summary, files_changed=files_changed)
        write_pointers = []
        try:
            if self.toolbox.git_is_repo(agent_id="router"):
                diff = self.toolbox.git_diff(agent_id="router")
                if diff.stdout:
                    change.kind = "patch"
                    change.patch_pointer = diff.stdout
        except Exception:
            pass
        if not change.patch_pointer:
            for c in reversed(plan_changes):
                if c.patch_pointer:
                    change.patch_pointer = c.patch_pointer
                    break

        # Deduplicate write pointers.
        seen_ptrs: set[str] = set()
        for p in all_write_pointers:
            s = str(p).strip()
            if not s or s in seen_ptrs:
                continue
            seen_ptrs.add(s)
            write_pointers.append(s)

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
                meta={
                    "reason": "chat_only",
                    "branch_id": self.branch_id,
                    "task_id": task_evt.id,
                    "route_level": route_level,
                    "requested_route_level": requested_route_level,
                    "agents": activated_agents_list,
                    "style": resolved_style,
                    "req_ptr": req_ptr,
                    "intent_ptr": intent_ptr,
                    "plan_ptr": plan_ptr,
                    "usecases_ptr": usecases_ptr,
                    "decisions_ptr": decisions_ptr,
                    "contract_ptr": contract_ptr,
                    "impl_blueprint_ptr": impl_blueprint_ptr,
                    "resume_from": resume_from,
                    "doctor_ptr": doctor_ptr,
                },
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
        #
        # Rationale:
        # - For higher routes (L2+), start with a "smoke" preflight to avoid running a full
        #   suite on a broken scaffold. Only after the repo is runnable do we escalate to
        #   the required profile (usually "full").
        qa_required_profile = "smoke" if route_level == "L0" else ("unit" if route_level == "L1" else "full")
        qa_profile = qa_required_profile
        if route_level in {"L2", "L3", "L4"}:
            qa_profile = "smoke"
        qa_commands = self._determine_test_commands(profile=qa_profile)

        envspec_ptr: Optional[str] = None
        envspec_commands: list[str] = []

        def maybe_envspec(*, reason: str, event_type: str) -> tuple[Optional[str], list[str]]:
            nonlocal envspec_ptr, envspec_commands
            if envspec_ptr is not None:
                return envspec_ptr, envspec_commands
            if env_engineer is None:
                return None, []
            activate_agent("env_engineer", reason="gate:envspec")
            try:
                env_user = (
                    f"Task:\n{task_text}\n\n"
                    f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                    f"Plan:\n{plan.model_dump_json()}\n\n"
                    f"ContextPacket:\n{ctx.model_dump_json()}\n\n"
                    "Problem:\n"
                    f"- QA profile (current) is {qa_profile} (required: {qa_required_profile})\n"
                    f"- Reason: {reason}\n\n"
                    "Request:\n"
                    "- Propose 1–4 shell commands that can be run locally to verify the repo is runnable (prefer build/lint/test).\n"
                    "- Use repo-root relative `cd` + `&&` when needed (Windows: `cd /d \"dir\" && ...`).\n"
                    "- Do not invent commands that do not exist in the repo (prefer using package.json scripts, Makefile, etc.).\n"
                    "- If truly nothing is runnable, return an empty commands list.\n"
                )
                if ctx_excerpts:
                    env_user = f"{env_user}\n\nRepoExcerpts:\n{ctx_excerpts}"

                env_msgs = self._messages_with_memory(
                    agent_id="env_engineer",
                    system=(
                        "你是环境工程师（env_engineer）。你的目标是让项目“能跑起来并可验证”。\n"
                        "只输出 JSON（不要 markdown），并严格匹配 EnvSpec schema：{commands: string[]}。\n"
                        "不要额外 key；不要最外层包一层对象。\n\n"
                        "规则：\n"
                        "- commands 仅包含可执行命令（1–4 条），优先 build/lint/test，其次 smoke。\n"
                        "- 命令必须尽量跨平台；如需进入子目录，按系统约定使用 `cd ... &&`。\n"
                        "- 不要输出破坏性命令（rm/del/format 等）。\n\n"
                        f"{workflow_hint}"
                    ),
                    user=env_user,
                )
                spec, _ = env_engineer.chat_json(schema=packs.EnvSpec, messages=env_msgs, user=env_user)

                # Normalize common shell patterns so downstream install detection works.
                normalized: list[str] = []
                for c in list(spec.commands or [])[:6]:
                    s = str(c).strip()
                    if not s:
                        continue
                    s = s.replace("\r", " ").replace("\n", " ").strip()
                    low = s.lower()
                    if low.startswith("cd ") and "&&" in s and '"' not in s:
                        left, rest = s.split("&&", 1)
                        parts = left.strip().split()
                        # cd [/d] dir
                        dir_part = ""
                        if len(parts) >= 3 and parts[1].lower() == "/d":
                            dir_part = parts[2]
                        elif len(parts) >= 2:
                            dir_part = parts[1]
                        if dir_part:
                            if os.name == "nt":
                                s = f'cd /d "{dir_part}" && {rest.strip()}'
                            else:
                                s = f'cd "{dir_part}" && {rest.strip()}'
                    normalized.append(s)

                if not normalized and qa_commands:
                    normalized = list(qa_commands)

                spec_to_save = spec.model_copy(update={"commands": normalized})
                envspec_ptr = self.artifacts.put_json(spec_to_save.model_dump(), suffix=".envspec.json", kind="envspec").to_pointer()
                envspec_commands = list(normalized)
                self._append_guarded(
                    event=new_event(
                        agent="env_engineer",
                        type=event_type,
                        summary="EnvSpec generated" if event_type == "ENV_UPDATED" else "EnvSpec generated (no QA commands detected)",
                        branch_id=self.branch_id,
                        pointers=[envspec_ptr],
                        meta={"route_level": route_level, "style": resolved_style, "commands": envspec_commands},
                    ),
                    activated_agents=activated_agents,
                )
            except Exception:
                return None, []
            return envspec_ptr, envspec_commands

        # L3/L4 require EnvSpec as an auditable deliverable.
        if route_level in {"L3", "L4"}:
            if env_engineer is None:
                raise RuntimeError("env_engineer is required for L3+ routes")
            maybe_envspec(reason="L3+ env gate", event_type="ENV_UPDATED")

        # On-demand env probing (L1/L2): if we can't find any runnable QA commands,
        # ask env_engineer to propose a minimal runnable command set.
        if (not mock_mode) and (not qa_commands) and env_engineer is not None:
            _ptr, cmds = maybe_envspec(reason="No QA commands detected by heuristics", event_type="ENV_PROBED")
            if cmds:
                qa_commands = cmds

        # If we still can't detect any runnable verification commands, bootstrap a minimal
        # project skeleton so QA has something real to execute (instead of "tests skipped").
        #
        # This avoids the awkward "PM asks the user to add package.json" loop: for write-enabled
        # runs, we should try to make the repo runnable ourselves.
        if (not mock_mode) and (not qa_commands) and route_level != "L0":
            try:
                activate_agent(primary_coder_id, reason="gate:bootstrap")
                bootstrap_role = f"{coder_role} (bootstrap)"
                bootstrap_user = (
                    f"Task:\n{task_text}\n\n"
                    f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                    f"Plan:\n{plan.model_dump_json()}\n\n"
                    f"ContextPacket:\n{ctx.model_dump_json()}\n\n"
                    "Problem:\n"
                    "- QA command discovery returned empty; the repo is not runnable/verifyable yet.\n"
                    f"- QA profile (current) is {qa_profile} (required: {qa_required_profile})\n\n"
                    "Request:\n"
                    "- Bootstrap the minimal project skeleton consistent with the existing folder structure.\n"
                    "- Add the missing build/test/lint scripts/config so at least one local verification command exists.\n"
                    "- Prefer the smallest working setup; do not add optional tooling unless required to make `build` pass.\n"
                    "- Windows compatibility matters for npm scripts (no single-quoted globs).\n"
                    "- If this is a TypeScript/Node repo, ensure `package.json` + `tsconfig.json` exist in the correct project dir(s).\n"
                    "- If this is a Python repo, ensure `pyproject.toml` and a minimal test/validation command exists.\n"
                    "\n"
                    "Return CodeChange JSON only."
                )
                if ctx_excerpts:
                    bootstrap_user = f"{bootstrap_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
                bootstrap_msgs = self._messages_with_memory(
                    agent_id=primary_coder_id,
                    system=(
                        f"You are {bootstrap_role}. Return JSON only for CodeChange with fields: "
                        "kind ('commit'|'patch'|'noop'), summary, writes? (list[{path,content}]), copies? (list[{src,dst}]), commit_hash?, patch_pointer?, files_changed[], blockers[]. "
                        "No extra keys. No wrapping object. No markdown.\n\n"
                        "Hard rules:\n"
                        "- Never write under `.vibe/` or `.git/` (those are internal system dirs).\n"
                        "- Never copy under `.vibe/` or `.git/`.\n"
                        "- Use only repo-root relative paths (no absolute paths / drive letters).\n"
                        "- Prefer 'writes' for file changes.\n"
                        "- Do not import new npm packages unless you ALSO add them to the correct `package.json` in writes.\n\n"
                        f"{workflow_hint}"
                    ),
                    user=bootstrap_user,
                )
                boot_change, _ = primary_coder.chat_json(schema=packs.CodeChange, messages=bootstrap_msgs, user=bootstrap_user)
                boot_change, boot_ptrs = self._materialize_code_change_with_repair(
                    change=boot_change,
                    actor_agent_id=primary_coder_id,
                    actor=primary_coder,
                    actor_role=bootstrap_role,
                    workflow_hint=workflow_hint,
                    activated_agents=activated_agents,
                    activate_agent=activate_agent,
                    route_level=route_level,
                    style=resolved_style,
                )
                self._append_guarded(
                    event=new_event(
                        agent=primary_coder_id,
                        type="PATCH_WRITTEN" if boot_change.kind == "patch" else "CODE_COMMIT",
                        summary=f"bootstrap: {boot_change.summary}".strip(),
                        branch_id=self.branch_id,
                        pointers=[p for p in [boot_change.patch_pointer, boot_change.commit_hash] if p] + list(boot_ptrs or []),
                        meta={"phase": "bootstrap", "files_changed": boot_change.files_changed, "route_level": route_level, "style": resolved_style},
                    ),
                    activated_agents=activated_agents,
                )
                try:
                    if boot_change.patch_pointer:
                        change.patch_pointer = boot_change.patch_pointer
                    if boot_change.files_changed:
                        change.files_changed = sorted(
                            {*(list(change.files_changed or [])), *(list(boot_change.files_changed or []))}
                        )
                except Exception:
                    pass
                qa_commands = self._determine_test_commands(profile=qa_profile)
            except Exception:
                # Best-effort: if bootstrap fails, continue; the later qa_no_commands gate will create a non-green checkpoint.
                pass

        def run_qa_step(*, phase: str) -> packs.TestReport:
            nonlocal qa_profile, qa_commands

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
                    meta={
                        "profile": qa_profile,
                        "phase": phase,
                        "commands": qa_commands,
                        "route_level": route_level,
                        "style": resolved_style,
                    },
                ),
                activated_agents=activated_agents,
            )
            report = self._run_tests(profile=qa_profile, commands=qa_commands)

            failure_fp = ""
            try:
                if not report.passed:
                    b0 = str((report.blockers or [""])[0] or "").strip()
                    extracted0 = self._extract_error_signals(b0, limit=12)
                    sig0 = self._failure_signature(report=report, extracted=extracted0, blocker_text=b0)
                    failure_fp = self._failure_fingerprint(signature=sig0)
            except Exception:
                failure_fp = ""

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
                        "failure_fingerprint": failure_fp,
                        "profile": qa_profile,
                        "phase": phase,
                        "commands": report.commands,
                        "route_level": route_level,
                        "style": resolved_style,
                    },
                ),
                activated_agents=activated_agents,
            )
            return report

        # Phase 1: preflight / minimal verification.
        report = run_qa_step(phase="preflight" if qa_profile != qa_required_profile else "final")

        # Phase 2 (L2+): if preflight passed, escalate to the required profile for full verification.
        if (not mock_mode) and report.passed and qa_profile != qa_required_profile:
            qa_profile = qa_required_profile
            qa_commands = self._determine_test_commands(profile=qa_profile)
            if (not qa_commands) and envspec_commands:
                qa_commands = list(envspec_commands)
            if (not qa_commands) and env_engineer is not None:
                _ptr, cmds = maybe_envspec(reason="No QA commands detected for final verification", event_type="ENV_PROBED")
                if cmds:
                    qa_commands = cmds
            report = run_qa_step(phase="final")

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
                meta={
                    "draft": True,
                    "branch_id": self.branch_id,
                    "task_id": task_evt.id,
                    "route_level": route_level,
                    "requested_route_level": requested_route_level,
                    "agents": activated_agents_list,
                    "qa_profile": qa_profile,
                    "qa_required_profile": qa_required_profile,
                    "style": resolved_style,
                    "req_ptr": req_ptr,
                    "intent_ptr": intent_ptr,
                    "plan_ptr": plan_ptr,
                    "usecases_ptr": usecases_ptr,
                    "decisions_ptr": decisions_ptr,
                    "contract_ptr": contract_ptr,
                    "impl_blueprint_ptr": impl_blueprint_ptr,
                    "resume_from": resume_from,
                    "doctor_ptr": doctor_ptr,
                },
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
                    "branch_id": self.branch_id,
                    "task_id": task_evt.id,
                    "route_level": route_level,
                    "requested_route_level": requested_route_level,
                    "agents": activated_agents_list,
                    "qa_profile": qa_profile,
                    "qa_required_profile": qa_required_profile,
                    "reason": "qa_no_commands",
                    "style": resolved_style,
                    "req_ptr": req_ptr,
                    "intent_ptr": intent_ptr,
                    "plan_ptr": plan_ptr,
                    "usecases_ptr": usecases_ptr,
                    "decisions_ptr": decisions_ptr,
                    "contract_ptr": contract_ptr,
                    "resume_from": resume_from,
                    "doctor_ptr": doctor_ptr,
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
            activate_agent("code_reviewer", reason="gate:review")
            if not reviewer:
                raise RuntimeError("code_reviewer is required for L2+ routes")

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
        if route_level in {"L2", "L3", "L4"} and report.passed:
            review, review_ptr = run_review()
            review_failed = (not review.passed) or bool(review.blockers)

        security_report: Optional[packs.RiskRegister] = None
        security_ptr: Optional[str] = None
        security_failed = False

        compliance_report: Optional[packs.ComplianceReport] = None
        compliance_ptr: Optional[str] = None
        compliance_failed = False

        perf_report: Optional[packs.PerfReport] = None
        perf_ptr: Optional[str] = None
        perf_failed = False

        def run_security() -> tuple[packs.RiskRegister, str, bool]:
            activate_agent("security", reason="gate:security")
            if not security:
                raise RuntimeError("security is required for L3+ routes")
            sec_user = (
                f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                f"DecisionPack:\n{decisions.model_dump_json() if decisions is not None else '{}'}\n\n"
                f"ContractPack:\n{contract.model_dump_json() if contract is not None else '{}'}\n\n"
                f"CodeChange:\n{change.model_dump_json()}\n\n"
                f"TestReport:\n{report.model_dump_json()}\n\n"
                f"ContextPacket:\n{ctx.model_dump_json()}"
            )
            if ctx_excerpts:
                sec_user = f"{sec_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
            base_system = (
                "你是安全审计（security）。你必须只列出 blockers 和 high 风险，其他不要展开。\n"
                "只输出 JSON（不要 markdown），并严格匹配 RiskRegister schema："
                "{passed: bool, blockers: RiskItem[], highs: RiskItem[]}。\n"
                "不要额外 key；不要最外层包一层对象。\n\n"
                "规则：\n"
                "- passed 只有在 blockers 和 highs 都为空时才为 true。\n"
                "- 每个 RiskItem 必须给出可定位的 pointers（文件片段/日志/契约）。\n"
                "- 如果没有任何风险，输出：{\"passed\":true,\"blockers\":[],\"highs\":[]}。\n\n"
                f"{workflow_hint}"
            )

            def _is_malformed(reg: packs.RiskRegister) -> bool:
                try:
                    items = list(reg.blockers or []) + list(reg.highs or [])
                    return len(items) == 1 and str(items[0].id or "") == "__malformed_output__"
                except Exception:
                    return False

            reg: Optional[packs.RiskRegister] = None
            last_err: Exception | None = None
            for attempt in range(2):
                try:
                    extra = ""
                    if attempt == 1:
                        extra = (
                            "\n\n强制格式（第二次尝试）：\n"
                            "- 只能输出一个 JSON 对象\n"
                            "- 所有 key 必须使用双引号\n"
                            "- 不允许输出数组/列表作为顶层\n"
                            "示例：{\"passed\":false,\"blockers\":[{\"id\":\"R1\",\"severity\":\"high\",\"title\":\"...\",\"description\":\"...\",\"pointers\":[\"path#L1-L2@sha256:...\"]}],\"highs\":[]}\n"
                        )
                    sec_msgs = self._messages_with_memory(
                        agent_id="security",
                        system=base_system + extra,
                        user=sec_user,
                    )
                    reg, _ = security.chat_json(schema=packs.RiskRegister, messages=sec_msgs, user=sec_user)
                    if reg is not None and not _is_malformed(reg):
                        break
                    last_err = RuntimeError("security output malformed")
                except Exception as e:
                    last_err = e
                    reg = None
                    continue

            if reg is None:
                reg = packs.RiskRegister(
                    passed=False,
                    blockers=[
                        packs.RiskItem(
                            id="__security_agent_error__",
                            severity="high",
                            title="Security agent failed to produce valid JSON",
                            description=str(last_err or "unknown error")[:800],
                            pointers=[],
                        )
                    ],
                    highs=[],
                )
            ptr = self.artifacts.put_json(reg.model_dump(), suffix=".security.json", kind="security").to_pointer()
            passed = bool(reg.passed) and not (reg.blockers or []) and not (reg.highs or [])
            self._append_guarded(
                event=new_event(
                    agent="security",
                    type="SEC_REVIEW_PASSED" if passed else "SEC_REVIEW_BLOCKED",
                    summary="Security review passed" if passed else "Security review blocked",
                    branch_id=self.branch_id,
                    pointers=[ptr],
                    meta={"route_level": route_level, "style": resolved_style},
                ),
                activated_agents=activated_agents,
            )
            return reg, ptr, passed

        def run_compliance() -> tuple[packs.ComplianceReport, str, bool]:
            activate_agent("compliance", reason="gate:compliance")
            if not compliance:
                raise RuntimeError("compliance is required for L4 routes")
            comp_user = (
                f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                f"DecisionPack:\n{decisions.model_dump_json() if decisions is not None else '{}'}\n\n"
                f"ContractPack:\n{contract.model_dump_json() if contract is not None else '{}'}\n\n"
                f"CodeChange:\n{change.model_dump_json()}\n\n"
                f"TestReport:\n{report.model_dump_json()}\n\n"
                f"ContextPacket:\n{ctx.model_dump_json()}"
            )
            if ctx_excerpts:
                comp_user = f"{comp_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
            comp_msgs = self._messages_with_memory(
                agent_id="compliance",
                system=(
                    "你是合规/隐私审计（compliance）。输出必须可执行：指出阻塞点并给出最小整改建议。\n"
                    "只输出 JSON（不要 markdown），并严格匹配 ComplianceReport schema：{passed: bool, notes: string[]}。\n"
                    "不要额外 key；不要最外层包一层对象。\n\n"
                    f"{workflow_hint}"
                ),
                user=comp_user,
            )
            cr, _ = compliance.chat_json(schema=packs.ComplianceReport, messages=comp_msgs, user=comp_user)
            ptr = self.artifacts.put_json(cr.model_dump(), suffix=".compliance.json", kind="compliance").to_pointer()
            passed = bool(cr.passed)
            self._append_guarded(
                event=new_event(
                    agent="compliance",
                    type="COMPLIANCE_PASSED" if passed else "COMPLIANCE_BLOCKED",
                    summary="Compliance passed" if passed else "Compliance blocked",
                    branch_id=self.branch_id,
                    pointers=[ptr],
                    meta={"route_level": route_level, "style": resolved_style},
                ),
                activated_agents=activated_agents,
            )
            return cr, ptr, passed

        def run_perf() -> tuple[packs.PerfReport, str, bool]:
            activate_agent("performance", reason="gate:performance")
            if not performance:
                raise RuntimeError("performance is required for L4 routes")
            perf_user = (
                f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                f"DecisionPack:\n{decisions.model_dump_json() if decisions is not None else '{}'}\n\n"
                f"ContractPack:\n{contract.model_dump_json() if contract is not None else '{}'}\n\n"
                f"CodeChange:\n{change.model_dump_json()}\n\n"
                f"TestReport:\n{report.model_dump_json()}\n\n"
                f"ContextPacket:\n{ctx.model_dump_json()}"
            )
            if ctx_excerpts:
                perf_user = f"{perf_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
            perf_msgs = self._messages_with_memory(
                agent_id="performance",
                system=(
                    "你是性能/资源评审（performance）。只输出 JSON 并匹配 PerfReport schema。\n"
                    "如果有性能回退/高风险，必须将 passed=false 并把原因写在 blockers/notes 中。\n\n"
                    f"{workflow_hint}"
                ),
                user=perf_user,
            )
            pr, _ = performance.chat_json(schema=packs.PerfReport, messages=perf_msgs, user=perf_user)
            ptr = self.artifacts.put_json(pr.model_dump(), suffix=".perf.json", kind="perf").to_pointer()
            passed = bool(getattr(pr, "passed", True)) and not (getattr(pr, "blockers", []) or [])
            self._append_guarded(
                event=new_event(
                    agent="performance",
                    type="PERF_BENCH_RUN" if passed else "PERF_REGRESSION",
                    summary="Performance OK" if passed else "Performance regression",
                    branch_id=self.branch_id,
                    pointers=[ptr],
                    meta={"route_level": route_level, "style": resolved_style},
                ),
                activated_agents=activated_agents,
            )
            return pr, ptr, passed

        if route_level in {"L3", "L4"} and report.passed and not review_failed:
            security_report, security_ptr, sec_passed = run_security()
            security_failed = not sec_passed

        if route_level == "L4" and report.passed and not review_failed and not security_failed:
            compliance_report, compliance_ptr, comp_passed = run_compliance()
            compliance_failed = not comp_passed
            if not compliance_failed:
                perf_report, perf_ptr, perf_passed = run_perf()
                perf_failed = not perf_passed

        if (not report.passed) or review_failed or security_failed or compliance_failed or perf_failed:
            base_max = int(getattr(self.config.behavior, "fix_loop_max_loops", 3) or 3)
            started_smoke_preflight = (not mock_mode) and (qa_profile != qa_required_profile)
            max_loops = self._compute_fix_loop_max_loops(
                base_max_loops=base_max,
                route_level=route_level,
                report=report,
                started_smoke_preflight=started_smoke_preflight,
            )
            loop = 0
            fix_history: list[str] = []
            harvest_cache: dict[str, tuple[dict[str, Any], str]] = {}
            sig_last: str = ""
            sig_repeat: int = 0

            # Verification cache: avoid re-running already-passing full command batches when
            # the last patch didn't touch their project dir. This improves fix-loop throughput
            # in multi-package Node repos (client+server).
            dirty_full_cmds: set[str] = set()
            last_full_cmds: list[str] = []
            try:
                known_node_dirs = [d.as_posix() if d.as_posix() not in {"", "."} else "." for d in self._find_node_project_dirs()]
            except Exception:
                known_node_dirs = ["."]
            if not known_node_dirs:
                known_node_dirs = ["."]

            def _cmd_node_dir(cmd: str) -> str:
                try:
                    d = self._shell_cd_dir(str(cmd or ""))
                    s = d.as_posix() if d.as_posix() not in {"", "."} else "."
                    return s or "."
                except Exception:
                    return "."

            def _file_node_dir(rel: str) -> str:
                r = str(rel or "").replace("\\", "/").lstrip("/")
                if not r:
                    return "."
                best = "."
                for d in known_node_dirs:
                    if d in {"", "."}:
                        continue
                    prefix = d.rstrip("/") + "/"
                    if r.startswith(prefix) and len(d) > len(best):
                        best = d
                return best

            def _is_global_node_config(rel: str) -> bool:
                r = str(rel or "").replace("\\", "/").lstrip("/")
                if not r:
                    return False
                if "/" in r:
                    return False
                name = r.lower()
                return name in {
                    "package.json",
                    "package-lock.json",
                    "pnpm-lock.yaml",
                    "yarn.lock",
                    "pnpm-workspace.yaml",
                    "tsconfig.json",
                    "eslint.config.js",
                    "eslint.config.cjs",
                    ".eslintrc",
                    ".eslintrc.json",
                    ".eslintrc.js",
                    ".eslintrc.cjs",
                }
            while loop < max_loops and ((not report.passed) or review_failed or security_failed or compliance_failed or perf_failed):
                loop += 1
                if not report.passed:
                    blocker_source = "tests"
                elif review_failed:
                    blocker_source = "review"
                elif security_failed:
                    blocker_source = "security"
                elif compliance_failed:
                    blocker_source = "compliance"
                else:
                    blocker_source = "performance"

                if blocker_source == "review":
                    blocker = ((review.blockers or []) if review is not None else [])[:1] or ["review blocked"]
                    blocker_text = blocker[0]
                elif blocker_source == "tests":
                    excerpt = self._test_failure_excerpt(report)
                    blocker_text = (report.blockers or ["tests failed"])[0]
                    if excerpt:
                        blocker_text = f"{blocker_text}\n\n{excerpt}"
                elif blocker_source == "security":
                    items = list((security_report.blockers or []) if security_report is not None else []) + list(
                        (security_report.highs or []) if security_report is not None else []
                    )
                    item = items[0] if items else None
                    if item is not None:
                        blocker_text = f"[{item.severity}] {item.title}\n{item.description}".strip()
                        ptrs = [str(p).strip() for p in (item.pointers or []) if str(p).strip()]
                        if ptrs:
                            blocker_text = f"{blocker_text}\n\nPointers:\n" + "\n".join(ptrs[:8])
                    else:
                        blocker_text = "Security review blocked"
                elif blocker_source == "compliance":
                    notes = list((compliance_report.notes or []) if compliance_report is not None else [])
                    blocker_text = "\n".join([str(n).strip() for n in notes if str(n).strip()][:8]) or "Compliance blocked"
                else:
                    blockers = list(getattr(perf_report, "blockers", []) or [])
                    notes = list(getattr(perf_report, "notes", []) or [])
                    lines = [str(n).strip() for n in (blockers + notes) if str(n).strip()]
                    blocker_text = "\n".join(lines[:8]) or "Performance blocked"

                harvest: Optional[dict[str, Any]] = None
                harvest_ptr: Optional[str] = None
                harvest_text: str = ""
                if blocker_source == "tests" and (not mock_mode):
                    try:
                        sig_cmd = self._failed_command_from_report(report)
                        sig_blocker = str((report.blockers or [""])[0] or "").strip()
                        sig = (sig_cmd + "|" + sig_blocker)[:320]
                    except Exception:
                        sig = ""

                    if sig:
                        if sig not in harvest_cache:
                            try:
                                h, h_ptr = self._build_test_failure_harvest(report=report, blocker_text=blocker_text)
                                if h_ptr:
                                    harvest_cache[sig] = (h, h_ptr)
                            except Exception:
                                pass
                        if sig in harvest_cache:
                            harvest, harvest_ptr = harvest_cache[sig]
                            harvest_text = self._format_harvest_for_prompt(harvest=harvest, pointer=harvest_ptr)

                if harvest and isinstance(harvest.get("signals"), list) and list(harvest.get("signals") or []):
                    extracted = [str(x).strip() for x in list(harvest.get("signals") or []) if str(x).strip()]
                else:
                    extracted = self._extract_error_signals(blocker_text, limit=25)

                # If we have a large batch of issues, allow a small extra fix-loop budget to
                # avoid "hit the cap then reset" patterns, while keeping a hard bound.
                try:
                    if blocker_source == "tests" and len(extracted) >= 30 and max_loops < 16:
                        max_loops = min(16, max_loops + 2)
                except Exception:
                    pass

                sig_now = ""
                fp_now = ""
                try:
                    sig_now = self._failure_signature(report=report, extracted=extracted[:12], blocker_text=blocker_text)
                    fp_now = self._failure_fingerprint(signature=sig_now) if sig_now else ""
                    if sig_now and sig_now == sig_last:
                        sig_repeat += 1
                    else:
                        sig_repeat = 0
                    sig_last = sig_now or sig_last
                except Exception:
                    sig_now = ""
                    fp_now = ""
                stagnating = sig_repeat >= 1
                stagnating_hard = sig_repeat >= 2

                # Escalation: on higher routes or repeated identical failures, refresh the
                # implementation_lead blueprint so the system can switch fixer/scope instead of looping.
                lead_refresh_key = "|".join(
                    [
                        blocker_source,
                        fp_now or blocker_text[:160],
                        "hard" if stagnating_hard else "first_high",
                    ]
                )
                if ((loop == 1 and route_level in {"L3", "L4"}) or stagnating_hard) and lead_refresh_key not in lead_fix_blueprint_keys:
                    try:
                        lead_fix_blueprint_keys.add(lead_refresh_key)
                        maybe_build_fix_blueprint(
                            reason="gate:blueprint_fix",
                            blocker_source=blocker_source,
                            blocker_text=blocker_text,
                            extracted=extracted,
                            report=report,
                        )
                    except Exception:
                        pass

                stagnation_help: str = ""
                stagnation_ptrs: list[str] = []
                if stagnating and blocker_source == "tests" and (not mock_mode):
                    # When we see the *same* failure signature twice, we likely aren't grounded enough.
                    # Run a tiny deterministic probe (ripgrep) to give fix agents concrete anchors.
                    try:
                        m = re.search(
                            r"cannot import name ['\"](?P<sym>[^'\"]+)['\"]",
                            blocker_text or "",
                            flags=re.IGNORECASE,
                        )
                        sym = (m.group("sym") or "").strip() if m else ""
                        if sym and len(sym) <= 80 and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", sym):
                            rr = self.toolbox.run_cmd(
                                agent_id="router",
                                cmd=["rg", "-n", "--max-count", "80", sym],
                                cwd=self.repo_root,
                                timeout_s=120,
                            )
                            stagnation_ptrs.extend([rr.stdout, rr.stderr, rr.meta])
                            out_text = self._artifact_head_text(rr.stdout, max_bytes=9000) if rr.stdout else ""
                            if out_text.strip():
                                stagnation_help = (
                                    "StagnationProbe（重复失败签名：补充一次 ripgrep 结果用于定位符号/引用位置；事实以 pointers 展开为准）：\n"
                                    f"Query: {sym}\n"
                                    f"StdoutPointer: {rr.stdout}\n"
                                    f"Excerpt:\n{out_text.strip()}"
                                )
                    except Exception:
                        stagnation_help = ""

                    # If we're still stuck (3x same signature) or we see a common env/tooling smell,
                    # collect extra deterministic probes so the next fix is evidence-driven.
                    try:
                        lowb = (blocker_text or "").lower()
                        env_smell = any(k in lowb for k in ["spawn unknown", "syscall: 'spawn'", "enoent", "eacces"])
                        if stagnating_hard or env_smell:
                            cmd_dir = Path(".")
                            try:
                                cmd_dir = self._shell_cd_dir(self._failed_command_from_report(report) or "")
                            except Exception:
                                cmd_dir = Path(".")

                            bin_ptr, bin_sum = self._node_bin_health_report(node_dir=cmd_dir, max_items=24)
                            if bin_ptr:
                                stagnation_ptrs.append(bin_ptr)
                                piece = (
                                    "EnvProbe（重复失败：检查 node_modules/.bin shim 健康度；事实以 pointers 展开为准）：\n"
                                    f"Workdir: {cmd_dir.as_posix() if cmd_dir.as_posix() else '.'}\n"
                                    f"BinHealthPointer: {bin_ptr}\n"
                                    f"Summary: {bin_sum}"
                                ).strip()
                                stagnation_help = (stagnation_help + "\n\n" if stagnation_help else "") + piece

                            rr = self.toolbox.run_cmd(
                                agent_id="router",
                                cmd=[
                                    "rg",
                                    "-n",
                                    "--max-count",
                                    "120",
                                    "--glob",
                                    "!**/node_modules/**",
                                    "--glob",
                                    "!**/.vibe/**",
                                    r"node_modules[\\/]\.bin",
                                    ".",
                                ],
                                cwd=self.repo_root,
                                timeout_s=120,
                            )
                            stagnation_ptrs.extend([rr.stdout, rr.stderr, rr.meta])
                            out_text = self._artifact_head_text(rr.stdout, max_bytes=9000) if rr.stdout else ""
                            if out_text.strip():
                                piece = (
                                    "RgProbe（定位是否有脚本硬编码 node_modules/.bin；事实以 pointers 展开为准）：\n"
                                    f"StdoutPointer: {rr.stdout}\n"
                                    f"Excerpt:\n{out_text.strip()}"
                                ).strip()
                                stagnation_help = (stagnation_help + "\n\n" if stagnation_help else "") + piece
                    except Exception:
                        pass
                sim_router = self._format_lessons_for_prompt(
                    self._similar_lessons_for_query(agent_id="router", query=blocker_text, limit=3)
                )

                incident: Optional[packs.IncidentPack] = None
                incident_ptr: Optional[str] = None
                if blocker_source == "tests":
                    try:
                        incident = self._incident_for_tests(report=report, blocker_text=blocker_text, activated_agents=available_agents)
                        incident_ptr = self.artifacts.put_json(
                            incident.model_dump(), suffix=".incident.json", kind="incident"
                        ).to_pointer()
                        self._append_guarded(
                            event=new_event(
                                agent="router",
                                type="INCIDENT_CREATED",
                                summary=f"Incident: {incident.summary}",
                                branch_id=self.branch_id,
                                pointers=[p for p in [incident_ptr, harvest_ptr] if p],
                                meta={
                                    "source": incident.source,
                                    "category": incident.category,
                                    "suggested_fix_agent": incident.suggested_fix_agent,
                                    "required_capabilities": incident.required_capabilities,
                                    "failure_fingerprint": fp_now,
                                    "failure_signature": sig_now[:600] if sig_now else "",
                                    "loop": loop,
                                    "route_level": route_level,
                                    "style": resolved_style,
                                },
                            ),
                            activated_agents=activated_agents,
                        )

                        try:
                            pinned: list[str] = []
                            pinned.extend([str(x).strip() for x in (incident.diagnosis or [])[:2] if str(x).strip()])
                            pinned.extend([str(x).strip() for x in (incident.next_steps or [])[:2] if str(x).strip()])
                            ptrs = [p for p in [incident_ptr, harvest_ptr] if p] + list(incident.evidence_pointers or [])[:8]
                            self._append_agent_memory(agent_id="router", kind="incident", summary=incident.summary, pinned=pinned, pointers=ptrs)
                        except Exception:
                            pass
                    except Exception:
                        incident = None
                        incident_ptr = None

                if blocker_source == "review":
                    fix_coder_id = self._select_fix_coder_for_review(review=review, activated_agents=available_agents)
                elif blocker_source == "tests":
                    fix_coder_id = (incident.suggested_fix_agent if incident is not None else None) or self._select_fix_coder_for_tests(
                        report=report, blocker_text=blocker_text, activated_agents=available_agents
                    )
                else:
                    fix_coder_id = self._select_fix_coder_for_text(text=blocker_text, activated_agents=available_agents)

                fix_coder_id = _preferred_fix_agent(fix_coder_id)
                if fix_coder_id == "coder_frontend" and coder_frontend is None:
                    fix_coder_id = "coder_backend"
                if fix_coder_id == "integration_engineer" and integrator is None:
                    fix_coder_id = "coder_backend"
                if (
                    stagnating
                    and blocker_source == "tests"
                    and integrator is not None
                    and fix_coder_id != "integration_engineer"
                    and not str(getattr(impl_blueprint, "recommended_fix_agent", "") or "").strip()
                ):
                    # When we're stuck, prefer an integrator to align modules/contracts/exports holistically.
                    fix_coder_id = "integration_engineer"
                activate_agent(fix_coder_id, reason="gate:fix_loop")

                fix_coder = (
                    coder_frontend
                    if fix_coder_id == "coder_frontend" and coder_frontend is not None
                    else integrator
                    if fix_coder_id == "integration_engineer" and integrator is not None
                    else coder_backend
                )

                fix_plan: Optional[packs.FixPlanPack] = None
                fix_plan_ptr: Optional[str] = None
                try:
                    can_triage = ops_engineer is not None and (mock_mode or self._api_key_available_for_agent("ops_engineer"))
                except Exception:
                    can_triage = False
                should_triage = (
                    can_triage
                    and ops_engineer is not None
                    and ((blocker_source != "tests") or (loop == 1) or stagnating or stagnating_hard)
                )
                if should_triage:
                    try:
                        activate_agent("ops_engineer", reason="gate:triage")
                        triage_user = (
                            f"BlockerSource: {blocker_source}\n"
                            f"Blocker:\n{blocker_text}\n\n"
                            f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                            f"UseCasePack:\n{usecases.model_dump_json() if usecases is not None else '{}'}\n\n"
                            f"DecisionPack:\n{decisions.model_dump_json() if decisions is not None else '{}'}\n\n"
                            f"ContractPack:\n{contract.model_dump_json() if contract is not None else '{}'}\n\n"
                            f"ReviewReport:\n{review.model_dump_json() if review is not None else '{}'}\n\n"
                            f"SecurityReport:\n{security_report.model_dump_json() if security_report is not None else '{}'}\n\n"
                            f"ComplianceReport:\n{compliance_report.model_dump_json() if compliance_report is not None else '{}'}\n\n"
                            f"PerfReport:\n{perf_report.model_dump_json() if perf_report is not None else '{}'}\n\n"
                            f"TestReport:\n{report.model_dump_json()}\n\n"
                            f"ContextPacket:\n{ctx.model_dump_json()}"
                        )
                        if extracted:
                            triage_user = f"{triage_user}\n\nExtractedErrors（同一失败命令的同类错误，建议一次修完）：\n" + "\n".join(
                                f"- {x}" for x in extracted[:20]
                            )
                        if harvest_text:
                            triage_user = f"{triage_user}\n\n{harvest_text}"
                        if stagnating:
                            triage_user = (
                                f"{triage_user}\n\nStagnationDetected: 连续两轮失败签名相同（loop={loop}）。"
                                "需要更换思路：严格基于 pointers/RepoExcerpts 做定位，避免重复尝试。\n"
                                f"FailureSignature: {sig_now[:380]}"
                            )
                        if stagnation_help:
                            triage_user = f"{triage_user}\n\n{stagnation_help}"
                        if sim_router:
                            triage_user = f"{triage_user}\n\n{sim_router}"
                        if ctx_excerpts:
                            triage_user = f"{triage_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
                        if blocker_source == "tests":
                            failure_excerpts = self._repo_excerpts_for_test_failure(report)
                            if failure_excerpts:
                                triage_user = f"{triage_user}\n\nFailureRepoExcerpts:\n{failure_excerpts}"

                        triage_msgs = self._messages_with_memory(
                            agent_id="ops_engineer",
                            system=(
                                "你是运维/排障工程师（ops_engineer）。目标：复现/诊断当前阻塞，并给出最小修复方案，指导 coder 在一次改动里批量修复同一失败命令中的同类错误。\n"
                                "只输出 JSON（不要 markdown），并严格匹配 FixPlanPack schema："
                                "{summary: string, root_causes: string[], repro_steps: string[], proposed_fixes: string[], files_to_check: string[], pointers: string[]}。\n"
                                "不要额外 key；不要最外层包一层对象。\n\n"
                                "规则：\n"
                                "- 只基于提供的错误信息/RepoExcerpts/FailureRepoExcerpts/pointers 做判断；不允许凭空编造文件/命令。\n"
                                "- repro_steps 要可执行且尽量少（1–4 条）；proposed_fixes 要具体可落地（指向文件/配置/依赖）。\n"
                                "- 不要让用户去做工程杂活；优先给出系统内部可自动完成的修复建议。\n\n"
                                f"{workflow_hint}"
                            ),
                            user=triage_user,
                        )
                        fix_plan, _ = ops_engineer.chat_json(schema=packs.FixPlanPack, messages=triage_msgs, user=triage_user)
                        fix_plan_ptr = self.artifacts.put_json(fix_plan.model_dump(), suffix=".fixplan.json", kind="fixplan").to_pointer()
                        self._append_guarded(
                            event=new_event(
                                agent="router",
                                type="STATE_TRANSITION",
                                summary=f"Fix-loop {loop}: triage ops_engineer",
                                branch_id=self.branch_id,
                                pointers=[p for p in [fix_plan_ptr, harvest_ptr] if p],
                                meta={"loop": loop, "blocker_source": blocker_source, "route_level": route_level, "style": resolved_style},
                            ),
                            activated_agents=activated_agents,
                        )
                    except Exception:
                        fix_plan = None
                        fix_plan_ptr = None

                lead_consult_text = ""
                lead_consult_ptrs: list[str] = []
                if impl_blueprint is not None and (stagnating or route_level in {"L3", "L4"}):
                    try:
                        lead_consult_text, lead_consult_ptrs = maybe_collect_lead_consults(
                            loop=loop,
                            blocker_source=blocker_source,
                            blocker_text=blocker_text,
                            extracted=extracted,
                            report=report,
                            failure_fingerprint=fp_now,
                        )
                    except Exception:
                        lead_consult_text, lead_consult_ptrs = "", []

                # Audit: record which agent is handling this fix-loop step.
                self._append_guarded(
                    event=new_event(
                        agent="router",
                        type="STATE_TRANSITION",
                        summary=f"Fix-loop {loop}: dispatch to {fix_coder_id}",
                        branch_id=self.branch_id,
                        pointers=list(lead_consult_ptrs or []),
                        meta={
                            "phase": "fix_loop",
                            "loop": loop,
                            "blocker_source": blocker_source,
                            "fix_agent": fix_coder_id,
                            "lead_recommended_fix_agent": str(getattr(impl_blueprint, "recommended_fix_agent", "") or "").strip()
                            if impl_blueprint is not None
                            else "",
                            "route_level": route_level,
                            "style": resolved_style,
                        },
                    ),
                    activated_agents=activated_agents,
                )

                fix_user = (
                    f"BlockerSource: {blocker_source}\n"
                    f"Blocker:\n{blocker_text}\n\n"
                    f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                    f"UseCasePack:\n{usecases.model_dump_json() if usecases is not None else '{}'}\n\n"
                    f"DecisionPack:\n{decisions.model_dump_json() if decisions is not None else '{}'}\n\n"
                    f"ContractPack:\n{contract.model_dump_json() if contract is not None else '{}'}\n\n"
                    f"ReviewReport:\n{review.model_dump_json() if review is not None else '{}'}\n\n"
                    f"SecurityReport:\n{security_report.model_dump_json() if security_report is not None else '{}'}\n\n"
                    f"ComplianceReport:\n{compliance_report.model_dump_json() if compliance_report is not None else '{}'}\n\n"
                    f"PerfReport:\n{perf_report.model_dump_json() if perf_report is not None else '{}'}\n\n"
                    f"TestReport:\n{report.model_dump_json()}\n\n"
                    f"ContextPacket:\n{ctx.model_dump_json()}"
                )
                if extracted:
                    fix_user = f"{fix_user}\n\nExtractedErrors（尽量一次修完同一失败命令中的这些问题）：\n" + "\n".join(
                        f"- {x}" for x in extracted[:20]
                    )
                if harvest_text:
                    fix_user = f"{fix_user}\n\n{harvest_text}"
                if stagnating:
                    fix_user = (
                        f"{fix_user}\n\nStagnationDetected: 连续两轮失败签名相同（loop={loop}）。"
                        "禁止重复尝试；必须打开 pointers/RepoExcerpts 定位根因并一次修完同类错误。\n"
                        f"FailureSignature: {sig_now[:380]}"
                    )
                if stagnating_hard:
                    fix_user = (
                        f"{fix_user}\n\nEnvPivot: 同一失败指纹已连续出现 3 次（loop={loop}，fingerprint={fp_now}）。"
                        "这通常不是“业务逻辑”问题，而是环境/命令/工程骨架/跨平台脚本问题（如 `.cmd`/`.exe`、"
                        "`node_modules/.bin` 占位 shim、PATH、lockfile/依赖安装）。"
                        "本轮优先修复这些工程级问题，让失败命令能稳定通过，再考虑其他改动。"
                    )
                if stagnation_help:
                    fix_user = f"{fix_user}\n\n{stagnation_help}"
                if sim_router:
                    fix_user = f"{fix_user}\n\n{sim_router}"
                sim_agent = self._format_lessons_for_prompt(
                    self._similar_lessons_for_query(agent_id=fix_coder_id, query=blocker_text, limit=2)
                )
                if sim_agent:
                    fix_user = f"{fix_user}\n\n（本工种相关经验）\n{sim_agent}"
                if impl_blueprint is not None:
                    lead_reason = str(getattr(impl_blueprint, "escalation_reason", "") or "").strip()
                    if lead_reason:
                        fix_user = f"{fix_user}\n\nImplementationLeadReason:\n{lead_reason}"
                if lead_consult_text:
                    fix_user = f"{fix_user}\n\n{lead_consult_text}"
                if ctx_excerpts:
                    fix_user = f"{fix_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
                if blocker_source == "tests":
                    failure_excerpts = self._repo_excerpts_for_test_failure(report)
                    if failure_excerpts:
                        fix_user = f"{fix_user}\n\nFailureRepoExcerpts:\n{failure_excerpts}"

                if fix_history:
                    fix_user = f"{fix_user}\n\nFixLoopHistory（已尝试的修复，避免重复）：\n" + "\n".join(fix_history[-6:])

                if fix_plan is not None:
                    fix_user = f"{fix_user}\n\nOpsFixPlan（运维排障建议；事实以 pointers 展开为准）：\n{fix_plan.model_dump_json()}"
                    if fix_plan_ptr:
                        fix_user = f"{fix_user}\n\nOpsFixPlanPointer: {fix_plan_ptr}"

                if blocker_source == "tests":
                    hint = self._fix_loop_autohint_for_tests(report=report, blocker_text=blocker_text)
                    if hint:
                        fix_user = f"{fix_user}\n\nAutoHint（硬逻辑，仅供参考，事实以 pointers 展开为准）：\n{hint}"
                    if incident is not None:
                        fix_user = f"{fix_user}\n\nIncidentPack:\n{incident.model_dump_json()}"
                        if incident_ptr:
                            fix_user = f"{fix_user}\n\nIncidentPointer: {incident_ptr}"

                auto_change: Optional[packs.CodeChange] = None
                if blocker_source == "tests":
                    auto_change = self._auto_code_change_for_test_failure(report=report, blocker_text=blocker_text)

                # Capability-gap fallback: consult an on-demand specialist for advice when
                # the incident signals an area the current fix agent likely doesn't cover.
                if auto_change is None and blocker_source == "tests" and incident is not None and "specialist" in self.config.agents:
                    try:
                        signal_caps = {"eslint", "typescript", "react", "vite", "db", "migration", "contract"}
                        req_caps = [c for c in (incident.required_capabilities or []) if str(c).strip().lower() in signal_caps]
                        fix_caps = self._agent_capabilities(fix_coder_id)
                        missing = [c for c in req_caps if str(c).strip().lower() not in fix_caps]
                        if missing and self._api_key_available_for_agent("specialist"):
                            sp = self._agent("specialist")
                            sp_system = (
                                "你是 on-demand specialist：用于在工作流卡住时给出“可执行”的排障建议与约束，"
                                "帮助主修复工种更快收敛。\n"
                                "只输出 JSON（不要 markdown），并严格匹配 ChatReply schema："
                                "{reply: string, suggested_actions: string[], pointers: string[]}。\n"
                                "规则：\n"
                                "- 必须基于 IncidentPack.evidence_pointers / blocker 文本提出建议；不要编造文件或命令。\n"
                                "- 建议必须可落地（例如：改哪个脚本/补哪个依赖/在哪里加导出）。\n"
                                "- reply 用中文，尽量短（<= 12 行）。\n\n"
                                f"{workflow_hint}"
                            )
                            sp_user = (
                                f"MissingCapabilities: {', '.join(missing)}\n\n"
                                f"IncidentPack:\n{incident.model_dump_json()}\n\n"
                                f"Blocker:\n{blocker_text}\n"
                            )
                            sp_msgs = self._messages_with_memory(agent_id="specialist", system=sp_system, user=sp_user)
                            advice, _ = sp.chat_json(schema=packs.ChatReply, messages=sp_msgs, user=sp_user)
                            advice_ptr = self.artifacts.put_json(advice.model_dump(), suffix=".specialist.json", kind="specialist").to_pointer()
                            self._append_guarded(
                                event=new_event(
                                    agent="router",
                                    type="STATE_TRANSITION",
                                    summary=f"Fix-loop {loop}: consulted specialist",
                                    branch_id=self.branch_id,
                                    pointers=[advice_ptr],
                                    meta={
                                        "phase": "fix_loop",
                                        "loop": loop,
                                        "action": "consult_specialist",
                                        "missing_capabilities": missing,
                                        "route_level": route_level,
                                        "style": resolved_style,
                                    },
                                ),
                                activated_agents=activated_agents,
                            )
                            fix_user = f"{fix_user}\n\nSpecialistAdvice:\n{advice.model_dump_json()}\n\nSpecialistPointer: {advice_ptr}"
                    except Exception:
                        pass

                fix_role = "Coder"
                if fix_coder_id == "coder_frontend":
                    fix_role = "Frontend Coder (React/TypeScript)"
                elif fix_coder_id == "integration_engineer":
                    fix_role = "Integration Engineer (align frontend/backend/contracts)"
                elif fix_coder_id == "coder_backend":
                    fix_role = "Backend Coder"
                fix_allow, fix_deny = _scope_for_fix_loop() if impl_blueprint is not None else ([], [])
                fix_scope_hint = ""
                try:
                    allow_short = [str(x).strip() for x in list(fix_allow or []) if str(x).strip()][:12]
                    deny_short = [str(x).strip() for x in list(fix_deny or []) if str(x).strip()][:12]
                except Exception:
                    allow_short, deny_short = [], []
                if allow_short or deny_short:
                    lines: list[str] = []
                    if allow_short:
                        lines.append("Allowed write paths/globs (MUST stay within):\n" + "\n".join([f"- {p}" for p in allow_short]))
                    if deny_short:
                        lines.append("Denied write paths/globs:\n" + "\n".join([f"- {p}" for p in deny_short]))
                    fix_scope_hint = "\n\nImplementationLeadWriteScope:\n" + "\n\n".join(lines)
                if auto_change is None:
                    fix_msgs = self._messages_with_memory(
                        agent_id=fix_coder_id,
                        system=(
                            f"You are {fix_role}. Fix the failing command. Batch-fix all related errors from the same failing command output. Return JSON only for CodeChange with fields: "
                            "kind ('commit'|'patch'|'noop'), summary, writes? (list[{path,content}]), copies? (list[{src,dst}]), commit_hash?, patch_pointer?, files_changed[], blockers[]. "
                            "Prefer 'writes' for file changes. No extra keys. No markdown.\n\n"
                            "Hard rules:\n"
                            "- Never write under `.vibe/` or `.git/` (those are internal system dirs).\n"
                            "- Never copy under `.vibe/` or `.git/`.\n"
                            "- Use only repo-root relative paths (no absolute paths / drive letters).\n"
                        "- Fix the failing command in the blocker; prefer fixing all ExtractedErrors in one go (still one failing command).\n"
                        "- Do not do architecture refactors during fix-loop.\n"
                            "- If you add/modify an import, ensure the target file exists or create it in writes.\n\n"
                            "- If you import a new npm package, add it to the correct `package.json` (dependencies/devDependencies).\n\n"
                            "- NPM scripts must be Windows-compatible: avoid single quotes around globs; prefer double quotes.\n"
                            "- Windows/Node: do NOT hardcode or spawn `node_modules/.bin/<tool>.exe` (may be a 0-byte shim). Prefer `npm run <script>` or `<tool>.cmd` on Windows.\n"
                            "- For env vars in scripts (e.g. NODE_ENV=production), prefer `cross-env` for cross-platform.\n\n"
                            f"{fix_scope_hint}\n\n{workflow_hint}"
                        ),
                    user=fix_user,
                )
                    change, _ = fix_coder.chat_json(schema=packs.CodeChange, messages=fix_msgs, user=fix_user)
                    change, write_pointers = self._materialize_code_change_with_repair(
                        change=change,
                        actor_agent_id=fix_coder_id,
                        actor=fix_coder,
                        actor_role=fix_role,
                        workflow_hint=workflow_hint,
                        activated_agents=activated_agents,
                        activate_agent=activate_agent,
                        route_level=route_level,
                        style=resolved_style,
                        write_allowlist=fix_allow,
                        write_denylist=fix_deny,
                    )
                else:
                    change, write_pointers = self._materialize_code_change(
                        auto_change,
                        actor_agent_id=fix_coder_id,
                        activated_agents=activated_agents,
                        activate_agent=activate_agent,
                        route_level=route_level,
                        style=resolved_style,
                        write_allowlist=fix_allow,
                        write_denylist=fix_deny,
                    )
                try:
                    evidence = change.commit_hash or change.patch_pointer or ""
                    files = [str(x).strip() for x in (change.files_changed or []) if str(x).strip()]
                    files_short = ", ".join(files[:6]) + (" ..." if len(files) > 6 else "")
                    fix_history.append(f"- loop {loop} {fix_coder_id}: {change.summary.strip()[:160]} | files: {files_short} | evidence: {evidence}")
                except Exception:
                    pass
                meta = {"blocker": blocker_text, "blocker_source": blocker_source, "route_level": route_level, "style": resolved_style}
                if fix_plan_ptr:
                    meta["fix_plan"] = fix_plan_ptr
                if auto_change is not None:
                    meta["auto_fix"] = True
                self._append_guarded(
                    event=new_event(
                        agent=fix_coder_id,
                        type="PATCH_WRITTEN" if change.kind == "patch" else "CODE_COMMIT",
                        summary=f"fix-loop {loop}: {change.summary}",
                        branch_id=self.branch_id,
                        pointers=[p for p in [change.patch_pointer, change.commit_hash] if p] + write_pointers,
                        meta=meta,
                    ),
                    activated_agents=activated_agents,
                )

                # "Eat a pit, grow wiser": persist a short, pointer-grounded lesson when we applied
                # a deterministic auto-fix so future runs (and future code generation) avoid it.
                if auto_change is not None:
                    try:
                        lesson_summary, lesson_pinned = self._autofix_lesson_text(change_summary=change.summary)
                        if lesson_summary:
                            ptrs: list[str] = []
                            try:
                                ptrs.extend(list(report.pointers or [])[:6])
                            except Exception:
                                pass
                            ptrs.extend([p for p in [change.patch_pointer, change.commit_hash] if p])
                            ptrs.extend(list(write_pointers or [])[:10])
                            self._append_agent_lesson(agent_id=fix_coder_id, summary=lesson_summary, pinned=lesson_pinned, pointers=ptrs)
                            self._append_agent_lesson(agent_id="router", summary=lesson_summary, pinned=lesson_pinned, pointers=ptrs)
                    except Exception:
                        pass

                prev_failed_cmd = self._failed_command_from_report(report) if blocker_source == "tests" else ""
                prev_ptrs: list[str] = [str(p).strip() for p in (report.pointers or []) if str(p).strip()]
                prev_signals = extracted[:8] if extracted else []

                qa_commands_full = self._determine_test_commands(profile=qa_profile)
                # Verification cache: only re-run "full" commands that are still dirty for the
                # touched project dir(s), rather than re-running the entire matrix every loop.
                if list(qa_commands_full) != last_full_cmds:
                    last_full_cmds = list(qa_commands_full)
                    dirty_full_cmds = set(qa_commands_full)
                    # Seed: commands that already passed before this loop don't need re-run.
                    try:
                        for r0 in list(report.results or []):
                            if getattr(r0, "passed", False) and str(getattr(r0, "command", "") or "") in dirty_full_cmds:
                                dirty_full_cmds.discard(str(getattr(r0, "command", "") or ""))
                    except Exception:
                        pass

                try:
                    touched_paths = list(change.files_changed or []) or [w.path for w in list(change.writes or [])]
                    touched_dirs: set[str] = set()
                    global_dirty = False
                    for rp in touched_paths[:80]:
                        relp = str(rp or "").replace("\\", "/").lstrip("/")
                        if not relp or relp.startswith(".vibe/") or relp.startswith(".git/"):
                            continue
                        if _is_global_node_config(relp):
                            global_dirty = True
                            break
                        touched_dirs.add(_file_node_dir(relp))
                    if global_dirty:
                        touched_dirs = set(known_node_dirs)
                        touched_dirs.add(".")
                    if touched_dirs:
                        for c0 in qa_commands_full:
                            if _cmd_node_dir(str(c0 or "")) in touched_dirs or "." in touched_dirs:
                                dirty_full_cmds.add(str(c0 or ""))
                except Exception:
                    pass

                qa_commands_focus = qa_commands_full
                if blocker_source == "tests":
                    try:
                        focus_text = blocker_text
                        if extracted:
                            focus_text = focus_text + "\n\nExtractedErrors:\n" + "\n".join(extracted[:30])
                        focus = self._focus_commands_for_test_failure(report=report, blocker_text=focus_text)
                        if focus:
                            qa_commands_focus = focus
                    except Exception:
                        qa_commands_focus = qa_commands_full

                self._append_guarded(
                    event=new_event(
                        agent="qa",
                        type="TEST_RUN",
                        summary=f"Fix-loop {loop}: verifying (focus)",
                        branch_id=self.branch_id,
                        pointers=[],
                        meta={
                            "profile": qa_profile,
                            "phase": "fix_focus",
                            "commands": qa_commands_focus,
                            "route_level": route_level,
                            "style": resolved_style,
                        },
                    ),
                    activated_agents=activated_agents,
                )
                focus_report = self._run_tests(profile=qa_profile, commands=qa_commands_focus)

                focus_fp = ""
                try:
                    if not focus_report.passed:
                        b0 = str((focus_report.blockers or [""])[0] or "").strip()
                        extracted0 = self._extract_error_signals(b0, limit=12)
                        sig0 = self._failure_signature(report=focus_report, extracted=extracted0, blocker_text=b0)
                        focus_fp = self._failure_fingerprint(signature=sig0)
                except Exception:
                    focus_fp = ""
                self._append_guarded(
                    event=new_event(
                        agent="qa",
                        type="TEST_PASSED" if focus_report.passed else "TEST_FAILED",
                        summary="Tests passed" if focus_report.passed else "Tests failed",
                        branch_id=self.branch_id,
                        pointers=focus_report.pointers,
                        meta={
                            "blockers": focus_report.blockers,
                            "failure_fingerprint": focus_fp,
                            "commands": focus_report.commands,
                            "loop": loop,
                            "profile": qa_profile,
                            "phase": "fix_focus",
                            "route_level": route_level,
                            "style": resolved_style,
                        },
                    ),
                    activated_agents=activated_agents,
                )

                report = focus_report
                try:
                    for r0 in list(focus_report.results or []):
                        if getattr(r0, "passed", False):
                            cmd0 = str(getattr(r0, "command", "") or "")
                            if cmd0 in dirty_full_cmds:
                                dirty_full_cmds.discard(cmd0)
                except Exception:
                    pass

                # If focus passed but we didn't run the full verification list, run only the "dirty"
                # subset once to surface the *next* blocker early (focus -> full), rather than
                # re-running the entire full matrix every loop.
                if (not mock_mode) and report.passed and qa_commands_focus != qa_commands_full:
                    qa_commands_full_to_run = [
                        c for c in list(qa_commands_full or []) if str(c or "") in dirty_full_cmds
                    ]
                    if qa_commands_full_to_run:
                        self._append_guarded(
                            event=new_event(
                                agent="qa",
                                type="TEST_RUN",
                                summary=f"Fix-loop {loop}: verifying (full)",
                                branch_id=self.branch_id,
                                pointers=[],
                                meta={
                                    "profile": qa_profile,
                                    "phase": "fix_full",
                                    "commands": qa_commands_full_to_run,
                                    "route_level": route_level,
                                    "style": resolved_style,
                                },
                            ),
                            activated_agents=activated_agents,
                        )
                        full_report = self._run_tests(profile=qa_profile, commands=qa_commands_full_to_run)

                        full_fp = ""
                        try:
                            if not full_report.passed:
                                b0 = str((full_report.blockers or [""])[0] or "").strip()
                                extracted0 = self._extract_error_signals(b0, limit=12)
                                sig0 = self._failure_signature(report=full_report, extracted=extracted0, blocker_text=b0)
                                full_fp = self._failure_fingerprint(signature=sig0)
                        except Exception:
                            full_fp = ""
                        self._append_guarded(
                            event=new_event(
                                agent="qa",
                                type="TEST_PASSED" if full_report.passed else "TEST_FAILED",
                                summary="Tests passed" if full_report.passed else "Tests failed",
                                branch_id=self.branch_id,
                                pointers=full_report.pointers,
                                meta={
                                    "blockers": full_report.blockers,
                                    "failure_fingerprint": full_fp,
                                    "commands": full_report.commands,
                                    "loop": loop,
                                    "profile": qa_profile,
                                    "phase": "fix_full",
                                    "route_level": route_level,
                                    "style": resolved_style,
                                },
                            ),
                            activated_agents=activated_agents,
                        )
                        report = full_report
                        try:
                            for r0 in list(full_report.results or []):
                                if getattr(r0, "passed", False):
                                    cmd0 = str(getattr(r0, "command", "") or "")
                                    if cmd0 in dirty_full_cmds:
                                        dirty_full_cmds.discard(cmd0)
                        except Exception:
                            pass

                # Persist a compact lesson when we moved past the previous failing command (or fully passed),
                # so future runs can avoid re-hitting the same pitfall.
                if blocker_source == "tests" and prev_failed_cmd:
                    new_failed_cmd = self._failed_command_from_report(report) if not report.passed else ""
                    should_lesson = report.passed or (new_failed_cmd and new_failed_cmd != prev_failed_cmd)
                    if should_lesson:
                        try:
                            root = ""
                            if fix_plan is not None and str(getattr(fix_plan, "summary", "") or "").strip():
                                root = str(getattr(fix_plan, "summary", "") or "").strip()
                            elif incident is not None and str(getattr(incident, "summary", "") or "").strip():
                                root = str(getattr(incident, "summary", "") or "").strip()
                            else:
                                root = "修复测试/构建阻塞"

                            lesson_summary = f"{prev_failed_cmd[:90]} -> {change.summary[:90]}".strip()
                            pinned: list[str] = []
                            if root:
                                pinned.append(f"原因/诊断: {root}")
                            pinned.extend([str(x).strip() for x in (prev_signals or []) if str(x).strip()][:4])
                            if fix_plan is not None:
                                try:
                                    pinned.extend([str(x).strip() for x in (fix_plan.root_causes or []) if str(x).strip()][:1])
                                except Exception:
                                    pass

                            ptrs: list[str] = []
                            ptrs.extend(prev_ptrs[:8])
                            ptrs.extend([p for p in [fix_plan_ptr, incident_ptr, change.patch_pointer, change.commit_hash] if p])
                            ptrs.extend(list(write_pointers or [])[:12])
                            self._append_agent_lesson(agent_id=fix_coder_id, summary=lesson_summary, pinned=pinned, pointers=ptrs)
                            self._append_agent_lesson(agent_id="router", summary=lesson_summary, pinned=pinned, pointers=ptrs)
                        except Exception:
                            pass

                # If we started with a smoke preflight (L2+), only consider the repo "passing"
                # after we also pass the required QA profile (usually "full").
                if (not mock_mode) and report.passed and qa_profile != qa_required_profile:
                    qa_profile = qa_required_profile
                    qa_commands_escalate = self._determine_test_commands(profile=qa_profile)
                    if (not qa_commands_escalate) and envspec_commands:
                        qa_commands_escalate = list(envspec_commands)
                    self._append_guarded(
                        event=new_event(
                            agent="qa",
                            type="TEST_RUN",
                            summary=f"Fix-loop {loop}: escalating verification ({qa_profile})",
                            branch_id=self.branch_id,
                            pointers=[],
                            meta={
                                "profile": qa_profile,
                                "phase": "final",
                                "commands": qa_commands_escalate,
                                "route_level": route_level,
                                "style": resolved_style,
                            },
                        ),
                        activated_agents=activated_agents,
                    )
                    report = self._run_tests(profile=qa_profile, commands=qa_commands_escalate)
                    self._append_guarded(
                        event=new_event(
                            agent="qa",
                            type="TEST_PASSED" if report.passed else "TEST_FAILED",
                            summary="Tests passed" if report.passed else "Tests failed",
                            branch_id=self.branch_id,
                            pointers=report.pointers,
                            meta={
                                "blockers": report.blockers,
                                "commands": report.commands,
                                "loop": loop,
                                "profile": qa_profile,
                                "phase": "final",
                                "route_level": route_level,
                                "style": resolved_style,
                            },
                        ),
                        activated_agents=activated_agents,
                    )

                review_failed = False
                review = None
                review_ptr = None
                if route_level in {"L2", "L3", "L4"} and report.passed:
                    review, review_ptr = run_review()
                    review_failed = (not review.passed) or bool(review.blockers)

                security_failed = False
                security_report = None
                security_ptr = None
                if route_level in {"L3", "L4"} and report.passed and not review_failed:
                    security_report, security_ptr, sec_passed = run_security()
                    security_failed = not sec_passed

                compliance_failed = False
                compliance_report = None
                compliance_ptr = None
                perf_failed = False
                perf_report = None
                perf_ptr = None
                if route_level == "L4" and report.passed and not review_failed and not security_failed:
                    compliance_report, compliance_ptr, comp_passed = run_compliance()
                    compliance_failed = not comp_passed
                    if not compliance_failed:
                        perf_report, perf_ptr, perf_passed = run_perf()
                        perf_failed = not perf_passed

            if (not report.passed) or review_failed or security_failed or compliance_failed or perf_failed:
                # Do not crash the CLI/UI: record a non-green checkpoint with the remaining blockers.
                blockers: list[str] = []
                if not report.passed:
                    blockers.extend([str(b) for b in (report.blockers or []) if str(b).strip()])
                if review_failed and review is not None:
                    blockers.extend([str(b) for b in (review.blockers or []) if str(b).strip()])
                if security_failed and security_report is not None:
                    items = list(security_report.blockers or []) + list(security_report.highs or [])
                    for it in items[:4]:
                        title = f"[{it.severity}] {it.title}".strip()
                        blockers.append(title)
                if compliance_failed and compliance_report is not None:
                    for n in list(compliance_report.notes or [])[:4]:
                        s = str(n).strip()
                        if s:
                            blockers.append(s)
                if perf_failed and perf_report is not None:
                    for n in list(getattr(perf_report, "blockers", []) or [])[:4]:
                        s = str(n).strip()
                        if s:
                            blockers.append(s)
                    for n in list(getattr(perf_report, "notes", []) or [])[:4]:
                        s = str(n).strip()
                        if s:
                            blockers.append(s)

                replan_required = False
                replan_trigger = ""
                try:
                    replan_required, replan_trigger = maybe_prepare_replan(
                        loop=loop,
                        blocker_source=blocker_source,
                        blocker_text=blocker_text,
                        extracted=extracted,
                        failure_fingerprint=fp_now,
                        report=report,
                        review=review,
                        security_report=security_report,
                        compliance_report=compliance_report,
                        perf_report=perf_report,
                        incident=incident,
                        incident_ptr=incident_ptr,
                        fix_plan=fix_plan,
                        fix_plan_ptr=fix_plan_ptr,
                        stagnating_hard=stagnating_hard,
                    )
                except Exception:
                    replan_required = False
                    replan_trigger = ""

                artifacts_blocked: List[str] = []
                artifacts_blocked.extend(
                    [p for p in [usecases_ptr, decisions_ptr, contract_ptr, review_ptr, security_ptr, compliance_ptr, perf_ptr, envspec_ptr, impl_blueprint_ptr] if p]
                )
                if change.patch_pointer:
                    artifacts_blocked.append(change.patch_pointer)
                artifacts_blocked.extend(report.pointers)

                repo_ref = "no-git"
                try:
                    repo_ref = self.toolbox.git_head_sha(agent_id="router")
                except Exception:
                    snap = self.checkpoints.snapshot_repo()
                    artifacts_blocked.append(snap.to_pointer())

                checkpoint_id = f"ckpt_{uuid4().hex[:12]}"
                restore_steps = (
                    [f"git checkout --detach {repo_ref}"]
                    if repo_ref != "no-git"
                    else [f"vibe checkpoint restore {checkpoint_id}"]
                )
                if replan_required:
                    restore_steps.append("重新运行 `vibe run`：将从本检查点恢复新的计划并继续实现/验证。")
                if change.patch_pointer:
                    restore_steps.append(f"（如需恢复未提交的变更）应用补丁：{change.patch_pointer}")

                cp = self.checkpoints.create(
                    checkpoint_id=checkpoint_id,
                    label=(req.summary if req is not None else task_text.strip().splitlines()[0][:120]),
                    repo_ref=repo_ref,
                    ledger_offset=self.ledger.count_lines(),
                    artifacts=artifacts_blocked,
                    green=False,
                    restore_steps=restore_steps,
                    meta={
                        "branch_id": self.branch_id,
                        "task_id": task_evt.id,
                        "route_level": route_level,
                        "requested_route_level": requested_route_level,
                        "agents": activated_agents_list,
                        "qa_profile": qa_profile,
                        "qa_required_profile": qa_required_profile,
                        "reason": "replan_required" if replan_required else "fix_loop_blockers",
                        "blockers": blockers[:20],
                        "replan_trigger": replan_trigger,
                        "style": resolved_style,
                        "req_ptr": req_ptr,
                        "intent_ptr": intent_ptr,
                        "plan_ptr": plan_ptr,
                        "usecases_ptr": usecases_ptr,
                        "decisions_ptr": decisions_ptr,
                        "contract_ptr": contract_ptr,
                        "impl_blueprint_ptr": impl_blueprint_ptr,
                        "resume_from": resume_from,
                        "doctor_ptr": doctor_ptr,
                    },
                )
                self._append_guarded(
                    event=new_event(
                        agent="router",
                        type="CHECKPOINT_CREATED",
                        summary=(
                            f"Created checkpoint {cp.id} (non-green, replan required)"
                            if replan_required
                            else f"Created checkpoint {cp.id} (non-green, blockers remain)"
                        ),
                        branch_id=self.branch_id,
                        pointers=artifacts_blocked,
                        meta={
                            "green": False,
                            "repo_ref": repo_ref,
                            "route_level": route_level,
                            "agents": activated_agents_list,
                            "style": resolved_style,
                            "reason": "replan_required" if replan_required else "fix_loop_blockers",
                            "blockers": blockers[:20],
                            "replan_trigger": replan_trigger,
                        },
                    ),
                    activated_agents=activated_agents,
                )
                return RunResult(checkpoint_id=cp.id, green=False)

        doc_ptr: Optional[str] = None
        release_ptr: Optional[str] = None
        ci_ptr: Optional[str] = None
        runbook_ptr: Optional[str] = None
        migration_ptr: Optional[str] = None

        if route_level in {"L3", "L4"}:
            activate_agent("doc_writer", reason="gate:docs")
            if doc_writer is None:
                raise RuntimeError("doc_writer is required for L3+ routes")
            doc_user = (
                f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                f"EnvSpec:\n{json.dumps({'commands': envspec_commands}, ensure_ascii=False)}\n\n"
                f"CodeChange:\n{change.model_dump_json()}\n\n"
                f"TestReport:\n{report.model_dump_json()}\n\n"
                f"ContextPacket:\n{ctx.model_dump_json()}\n\n"
                "Request:\n"
                "- 列出本次交付需要更新/确认的文档文件路径（例如 README.md / docs/... / CHANGELOG.md）。\n"
                "- 如果 README 里缺少“安装/启动/最小验证步骤”，必须把 README.md 包含进 files。\n"
                "- 如果存在 mock 数据源/可切换数据源，也必须要求 README 写清楚。\n"
            )
            if ctx_excerpts:
                doc_user = f"{doc_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
            doc_msgs = self._messages_with_memory(
                agent_id="doc_writer",
                system=(
                    "你是文档工程师（doc_writer）。目标：让用户拿到项目后能立即运行并验证。\n"
                    "只输出 JSON（不要 markdown），并严格匹配 DocPack schema：{files: string[]}。\n"
                    "不要额外 key；不要最外层包一层对象。\n\n"
                    f"{workflow_hint}"
                ),
                user=doc_user,
            )
            doc_pack, _ = doc_writer.chat_json(schema=packs.DocPack, messages=doc_msgs, user=doc_user)
            doc_ptr = self.artifacts.put_json(doc_pack.model_dump(), suffix=".docs.json", kind="docs").to_pointer()
            self._append_guarded(
                event=new_event(
                    agent="doc_writer",
                    type="DOC_UPDATED",
                    summary="Docs checklist prepared",
                    branch_id=self.branch_id,
                    pointers=[doc_ptr],
                    meta={"route_level": route_level, "style": resolved_style},
                ),
                activated_agents=activated_agents,
            )

            activate_agent("release_manager", reason="gate:release_notes")
            if release_manager is None:
                raise RuntimeError("release_manager is required for L3+ routes")
            rel_user = (
                f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                f"CodeChange:\n{change.model_dump_json()}\n\n"
                f"TestReport:\n{report.model_dump_json()}\n\n"
                "Request:\n"
                "- 给出一个版本号（可使用 0.1.x）和简明 changelog。\n"
                "- 不要真的打 tag；只输出 ReleasePack。\n"
            )
            rel_msgs = self._messages_with_memory(
                agent_id="release_manager",
                system=(
                    "你是发布经理（release_manager）。只输出 JSON 并严格匹配 ReleasePack schema：{version: string, changelog: string[]}。\n"
                    "不要额外 key；不要最外层包一层对象。\n\n"
                    f"{workflow_hint}"
                ),
                user=rel_user,
            )
            rel_pack, _ = release_manager.chat_json(schema=packs.ReleasePack, messages=rel_msgs, user=rel_user)
            release_ptr = self.artifacts.put_json(rel_pack.model_dump(), suffix=".release.json", kind="release").to_pointer()
            self._append_guarded(
                event=new_event(
                    agent="release_manager",
                    type="CHANGELOG_UPDATED",
                    summary=f"Release notes prepared ({rel_pack.version})",
                    branch_id=self.branch_id,
                    pointers=[release_ptr],
                    meta={"route_level": route_level, "style": resolved_style, "version": rel_pack.version},
                ),
                activated_agents=activated_agents,
            )

            # DevOps is on-demand in L3+: only run when CI configs exist or user requests release/CI work.
            ci_markers = [
                self.repo_root / ".github" / "workflows",
                self.repo_root / ".gitlab-ci.yml",
                self.repo_root / "azure-pipelines.yml",
                self.repo_root / "Jenkinsfile",
            ]
            needs_ci = risks.touches_release or any(m.exists() for m in ci_markers) or ("ci" in (task_text or "").lower())
            if needs_ci and devops is not None:
                activate_agent("devops", reason="gate:ci")
                ci_user = (
                    f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                    f"EnvSpec:\n{json.dumps({'commands': envspec_commands}, ensure_ascii=False)}\n\n"
                    f"TestReport:\n{report.model_dump_json()}\n\n"
                    "Request:\n"
                    "- 给出 CI 建议（缓存/门禁/命令），保持最小且可落地。\n"
                )
                ci_msgs = self._messages_with_memory(
                    agent_id="devops",
                    system=(
                        "你是 DevOps。只输出 JSON 并严格匹配 CIPack schema：{notes: string[]}。\n"
                        "不要额外 key；不要最外层包一层对象。\n\n"
                        f"{workflow_hint}"
                    ),
                    user=ci_user,
                )
                ci_pack, _ = devops.chat_json(schema=packs.CIPack, messages=ci_msgs, user=ci_user)
                ci_ptr = self.artifacts.put_json(ci_pack.model_dump(), suffix=".ci.json", kind="ci").to_pointer()
                self._append_guarded(
                    event=new_event(
                        agent="devops",
                        type="CI_UPDATED",
                        summary="CI notes prepared",
                        branch_id=self.branch_id,
                        pointers=[ci_ptr],
                        meta={"route_level": route_level, "style": resolved_style},
                    ),
                    activated_agents=activated_agents,
                )

        if route_level == "L4":
            activate_agent("support_engineer", reason="gate:runbook")
            if support_engineer is None:
                raise RuntimeError("support_engineer is required for L4 routes")
            rb_user = (
                f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                f"EnvSpec:\n{json.dumps({'commands': envspec_commands}, ensure_ascii=False)}\n\n"
                f"ReleasePackPointer:\n{release_ptr or ''}\n\n"
                "Request:\n"
                "- 生成最小可用的运维/排障 Runbook 分节标题列表。\n"
            )
            rb_msgs = self._messages_with_memory(
                agent_id="support_engineer",
                system=(
                    "你是运维支持工程师（support_engineer）。只输出 JSON 并严格匹配 RunbookPack schema：{sections: string[]}。\n"
                    "不要额外 key；不要最外层包一层对象。\n\n"
                    f"{workflow_hint}"
                ),
                user=rb_user,
            )
            rb_pack, _ = support_engineer.chat_json(schema=packs.RunbookPack, messages=rb_msgs, user=rb_user)
            runbook_ptr = self.artifacts.put_json(rb_pack.model_dump(), suffix=".runbook.json", kind="runbook").to_pointer()
            self._append_guarded(
                event=new_event(
                    agent="support_engineer",
                    type="RUNBOOK_UPDATED",
                    summary="Runbook prepared",
                    branch_id=self.branch_id,
                    pointers=[runbook_ptr],
                    meta={"route_level": route_level, "style": resolved_style},
                ),
                activated_agents=activated_agents,
            )

            if risks.touches_migration:
                activate_agent("data_engineer", reason="gate:migration")
                if data_engineer is None:
                    raise RuntimeError("data_engineer is required for migration tasks in L4")
                mig_user = (
                    f"Task:\n{task_text}\n\n"
                    f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                    f"DecisionPack:\n{decisions.model_dump_json() if decisions is not None else '{}'}\n\n"
                    f"ContextPacket:\n{ctx.model_dump_json()}"
                )
                mig_msgs = self._messages_with_memory(
                    agent_id="data_engineer",
                    system=(
                        "你是数据工程师（data_engineer）。输出迁移计划和回滚步骤。\n"
                        "只输出 JSON 并严格匹配 MigrationPlan schema：{steps: string[], rollback_steps: string[]}。\n"
                        "不要额外 key；不要最外层包一层对象。\n\n"
                        f"{workflow_hint}"
                    ),
                    user=mig_user,
                )
                mig_plan, _ = data_engineer.chat_json(schema=packs.MigrationPlan, messages=mig_msgs, user=mig_user)
                migration_ptr = self.artifacts.put_json(mig_plan.model_dump(), suffix=".migration.json", kind="migration").to_pointer()
                self._append_guarded(
                    event=new_event(
                        agent="data_engineer",
                        type="DB_MIGRATION_PLANNED",
                        summary="Migration plan prepared",
                        branch_id=self.branch_id,
                        pointers=[migration_ptr],
                        meta={"route_level": route_level, "style": resolved_style},
                    ),
                    activated_agents=activated_agents,
                )

        # Create green checkpoint
        artifacts: List[str] = []
        artifacts.extend(
            [
                p
                for p in [
                    usecases_ptr,
                    decisions_ptr,
                    contract_ptr,
                    review_ptr,
                    security_ptr,
                    compliance_ptr,
                    perf_ptr,
                    envspec_ptr,
                    doc_ptr,
                    release_ptr,
                    ci_ptr,
                    runbook_ptr,
                    migration_ptr,
                ]
                if p
            ]
        )
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
            meta={
                "branch_id": self.branch_id,
                "task_id": task_evt.id,
                "route_level": route_level,
                "requested_route_level": requested_route_level,
                "agents": activated_agents_list,
                "qa_profile": qa_profile,
                "qa_required_profile": qa_required_profile,
                "style": resolved_style,
                "req_ptr": req_ptr,
                "intent_ptr": intent_ptr,
                "plan_ptr": plan_ptr,
                "usecases_ptr": usecases_ptr,
                "decisions_ptr": decisions_ptr,
                "contract_ptr": contract_ptr,
                "impl_blueprint_ptr": impl_blueprint_ptr,
                "resume_from": resume_from,
                "doctor_ptr": doctor_ptr,
                "deliverables": {
                    "envspec": envspec_ptr,
                    "security": security_ptr,
                    "compliance": compliance_ptr,
                    "perf": perf_ptr,
                    "docs": doc_ptr,
                    "release": release_ptr,
                    "ci": ci_ptr,
                    "runbook": runbook_ptr,
                    "migration": migration_ptr,
                },
            },
        )
        self._append_guarded(
            event=new_event(
                agent="router",
                type="CHECKPOINT_CREATED",
                summary=f"Created green checkpoint {cp.id}",
                branch_id=self.branch_id,
                pointers=artifacts,
                meta={
                    "green": True,
                    "repo_ref": repo_ref,
                    "route_level": route_level,
                    "requested_route_level": requested_route_level,
                    "agents": activated_agents_list,
                    "style": resolved_style,
                },
            ),
            activated_agents=activated_agents,
        )

        return RunResult(checkpoint_id=cp.id, green=True)
