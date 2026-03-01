from __future__ import annotations

import json
import os
import importlib.util
import re
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
from vibe.storage.checkpoints import CheckpointsStore
from vibe.storage.ledger import Ledger
from vibe.storage.ledger import ledger_path
from vibe.toolbox import Toolbox
from vibe.routes import DiffStats, decide_route, detect_risks
from vibe.routes import RiskSignals
from vibe.context import append_memory_record, effective_context_config, read_memory_records
from vibe.delivery import augment_plan, augment_requirement_pack
from vibe.schemas.memory import ChatDigest, MemoryRecord
from vibe.style import normalize_style, style_workflow_hint
from vibe.text import decode_bytes


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
        for rel in [
            ".vibe/manifests/project_manifest.md",
            ".vibe/manifests/run_manifest.md",
            ".vibe/manifests/repo_overview.md",
            "README.md",
        ]:
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

    def _contains_any(self, text: str, needles: List[str]) -> bool:
        t = text or ""
        for n in needles:
            if n and n in t:
                return True
        return False

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

    def _agents_for_route(self, route_level: packs.RouteLevel) -> list[str]:
        profile = (self.config.routes.levels or {}).get(route_level)
        agents = list(profile.agents) if profile else []
        if not agents:
            # Backward compatible fallback: treat as L1 minimal set.
            agents = ["pm", "router", "coder_backend", "qa"] if route_level != "L0" else ["router", "coder_backend", "qa"]

        # Helper coders (hard logic): keep routes fixed while enabling better error triage.
        # These agents are only invoked when needed; activating them upfront keeps ledger
        # permissions auditable without mid-run escalation.
        if route_level != "L0":
            for extra in ["coder_frontend", "env_engineer"]:
                if extra in self.config.agents and extra not in agents:
                    agents.append(extra)
        if route_level in {"L2", "L3", "L4"}:
            for extra in ["integration_engineer"]:
                if extra in self.config.agents and extra not in agents:
                    agents.append(extra)
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
        stdout_text = self._artifact_tail_text(failed.stdout, max_bytes=16000) if failed.stdout else ""
        stderr_text = self._artifact_tail_text(failed.stderr, max_bytes=16000) if failed.stderr else ""
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

        blob = "\n\n".join(out).strip()
        if len(blob) > max_chars:
            blob = blob[:max_chars] + "\n…（摘录过长，已截断）…"
        return blob

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

        return ""

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
        if not change.writes:
            return

        exts = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".d.ts"]
        js_like = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".d.ts"}

        def norm(p: str) -> str:
            return (p or "").replace("\\", "/").lstrip("/")

        planned = {norm(w.path) for w in change.writes if (w.path or "").strip()}
        planned_lower = {p.lower() for p in planned}
        planned_text_by_path_lower = {norm(w.path).lower(): (w.content or "") for w in change.writes if (w.path or "").strip()}

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

    def _materialize_code_change(self, change: packs.CodeChange, *, actor_agent_id: str = "coder_backend") -> Tuple[packs.CodeChange, List[str]]:
        write_pointers: List[str] = []
        if change.writes:
            for w in change.writes:
                rel = (w.path or "").replace("\\", "/").lstrip("/")
                if rel.startswith(".vibe/") or rel.startswith(".git/"):
                    raise RuntimeError(f"Refusing to write internal path: {w.path}")
                ptr = self.toolbox.write_file(agent_id=actor_agent_id, path=w.path, content=w.content)
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
                return self._materialize_code_change(current, actor_agent_id=actor_agent_id)
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
                    "- 优先使用 `writes` 给出完整文件内容；不要依赖 patch 指针。\n"
                    "- 如果你新增了相对 import（例如 `../controllers/x`），必须在 writes 里创建对应文件（或改成指向已存在文件）。\n"
                    "- 如果你新增了外部依赖（例如 `import axios from 'axios'`），必须在对应的 `package.json`（dependencies/devDependencies）里声明它。\n"
                    "- 保持原意不变：只修复路径/可落地性问题，不要引入大重构。\n\n"
                    "请只输出符合 CodeChange schema 的 JSON（不要 markdown，不要包裹对象）。\n\n"
                    f"上一个 CodeChange（供参考）：\n{prev}\n"
                )
                repair_system = (
                    f"You are {actor_role}. Return JSON only for CodeChange with fields: "
                    "kind ('commit'|'patch'|'noop'), summary, writes? (list[{path,content}]), commit_hash?, patch_pointer?, files_changed[], blockers[]. "
                    "No extra keys. No markdown.\n\n"
                    "Hard rules:\n"
                    "- Do not write under `.vibe/` or `.git/`.\n"
                    "- Use only repo-root relative paths.\n"
                    "- Prefer `writes` over `patch_pointer`.\n\n"
                    f"{workflow_hint}"
                )
                repair_msgs = self._messages_with_memory(agent_id=actor_agent_id, system=repair_system, user=repair_user)
                current, _ = actor.chat_json(schema=packs.CodeChange, messages=repair_msgs, user=repair_user)

        raise RuntimeError(f"Failed to materialize CodeChange after repair attempts. Last error: {last_err}")

    def _append_agent_lesson(self, *, agent_id: str, summary: str, pinned: list[str], pointers: list[str]) -> None:
        """
        Lightweight "eat a pit, grow wiser" mechanism:
        append a structured digest into `.vibe/views/<agent>/memory.jsonl` so future prompts can reuse it.

        This stays within the "derived info + pointers" rule: the digest contains conclusions and guardrails,
        and the pointers reference the concrete evidence (artifacts / file pointers / commits).
        """

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
        record = MemoryRecord(ts=ts, agent_id=agent_id, kind="chat_digest", digest=digest, pointers=list(pointers or [])[:16])
        append_memory_record(mem_path, record)

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
        return ("", [])

    def _determine_test_commands(self, *, profile: str) -> List[str]:
        if os.getenv("VIBE_MOCK_MODE", "").strip() == "1":
            return ["mock"]

        is_py = (self.repo_root / "pyproject.toml").exists() or (self.repo_root / "tests").exists()
        node_dirs = self._find_node_project_dirs()
        is_node = bool(node_dirs)
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
        report_cmds: List[str] = list(cmds)

        # Best-effort: for Node projects, ensure deps exist before running build/lint/test.
        # This avoids failures like "cannot find module" on fresh checkouts.
        try:
            node_dirs: list[Path] = []
            for c in report_cmds:
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
                    report_cmds = pre_cmds + report_cmds
                    return packs.TestReport(commands=report_cmds, results=results, passed=False, blockers=blockers, pointers=pointers)

            if pre_cmds:
                report_cmds = pre_cmds + report_cmds
        except Exception:
            pass

        for cmd in cmds:
            r = self.toolbox.run_cmd(agent_id="qa", cmd=cmd, cwd=self.repo_root, timeout_s=1800)
            passed = r.returncode == 0
            results.append(
                packs.TestResult(command=cmd, returncode=r.returncode, passed=passed, stdout=r.stdout, stderr=r.stderr, meta=r.meta)
            )
            pointers.extend([r.stdout, r.stderr, r.meta])
            if not passed:
                stderr_tail = self._compact_error_excerpt(self._artifact_tail_text(r.stderr, max_bytes=12000))
                stdout_tail = self._compact_error_excerpt(self._artifact_tail_text(r.stdout, max_bytes=12000))
                excerpt = stderr_tail or stdout_tail
                if excerpt:
                    blockers.append(f"Command failed: {cmd}\n\n{excerpt}")
                else:
                    blockers.append(f"Command failed: {cmd}")

        return packs.TestReport(commands=report_cmds, results=results, passed=all(x.passed for x in results), blockers=blockers, pointers=pointers)

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
        requested_route_level = decision.route_level
        route_level = requested_route_level
        route_reasons = list(decision.reasons or [])

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
                    "requested_route_level": requested_route_level,
                    "reasons": route_reasons,
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
                meta={
                    "route_level": route_level,
                    "requested_route_level": requested_route_level,
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
        pm = self._agent("pm") if "pm" in activated_agents else None
        req_analyst = self._agent("requirements_analyst") if "requirements_analyst" in activated_agents else None
        architect = self._agent("architect") if "architect" in activated_agents else None
        api_confirm = self._agent("api_confirm") if "api_confirm" in activated_agents else None
        env_engineer = self._agent("env_engineer") if "env_engineer" in activated_agents else None
        coder_backend = self._agent("coder_backend")
        coder_frontend = self._agent("coder_frontend") if "coder_frontend" in activated_agents else None
        integrator = self._agent("integration_engineer") if "integration_engineer" in activated_agents else None
        reviewer = self._agent("code_reviewer") if "code_reviewer" in activated_agents else None
        security = self._agent("security") if "security" in activated_agents else None
        compliance = self._agent("compliance") if "compliance" in activated_agents else None
        performance = self._agent("performance") if "performance" in activated_agents else None
        data_engineer = self._agent("data_engineer") if "data_engineer" in activated_agents else None
        devops = self._agent("devops") if "devops" in activated_agents else None
        doc_writer = self._agent("doc_writer") if "doc_writer" in activated_agents else None
        release_manager = self._agent("release_manager") if "release_manager" in activated_agents else None
        support_engineer = self._agent("support_engineer") if "support_engineer" in activated_agents else None

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

        if route_level in {"L2", "L3", "L4"}:
            if req_analyst is None:
                raise RuntimeError("requirements_analyst must be activated for L2+ routes")
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

            if architect is None:
                raise RuntimeError("architect must be activated for L2+ routes")
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

            if risks.contract_change or risks.touches_external_api:
                if api_confirm is None:
                    raise RuntimeError("api_confirm must be activated for contract/external API changes")
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
                "你是调度器 Router：把需求拆成最多 5 个可执行任务。\n"
                "只输出 JSON（不要 markdown），并严格匹配 Plan schema：{tasks:[{id,title,agent,description}]}。\n"
                "不要额外 key；不要最外层包一层对象。\n\n"
                "规划规则：\n"
                "- tasks <= 5；每个 task 必须可落地、可验收。\n"
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
        plan = augment_plan(plan, req=req, task_text=task_text, activated_agents=activated_agents)
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

        primary_coder_id = self._select_primary_coder(task_text=task_text, risks=risks, activated_agents=activated_agents)
        if primary_coder_id == "coder_frontend" and coder_frontend is None:
            primary_coder_id = "coder_backend"
        if primary_coder_id == "integration_engineer" and integrator is None:
            primary_coder_id = "coder_backend"

        primary_coder = (
            coder_frontend
            if primary_coder_id == "coder_frontend" and coder_frontend is not None
            else integrator
            if primary_coder_id == "integration_engineer" and integrator is not None
            else coder_backend
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
        coder_role = "Coder"
        if primary_coder_id == "coder_frontend":
            coder_role = "Frontend Coder (React/TypeScript)"
        elif primary_coder_id == "integration_engineer":
            coder_role = "Integration Engineer (align frontend/backend/contracts)"
        elif primary_coder_id == "coder_backend":
            coder_role = "Backend Coder"

        coder_msgs = self._messages_with_memory(
            agent_id=primary_coder_id,
            system=(
                f"You are {coder_role}. Return JSON only for CodeChange with fields: "
                "kind ('commit'|'patch'|'noop'), summary, writes? (list[{path,content}]), commit_hash?, patch_pointer?, files_changed[], blockers[]. "
                "Prefer 'writes' for file changes (especially when starting from an empty repo). "
                "Each writes item must include the full file content. No extra keys. No markdown.\n\n"
                "Hard rules:\n"
                "- Never write under `.vibe/` or `.git/` (those are internal system dirs).\n"
                "- Use only repo-root relative paths (no absolute paths / drive letters).\n"
                "- Do not introduce new modules/folders unless you ALSO create them in writes.\n"
                "- Do not import new npm packages unless you ALSO add them to the correct `package.json` (dependencies/devDependencies) in writes.\n"
                "- Do not do large refactors; prefer the smallest coherent change set.\n"
                "- If you change exports/imports, ensure all references stay consistent.\n"
                 "- For TypeScript repos, aim to make `npm run build` pass in affected node project(s).\n"
                 "- If you add a Vite app, include `index.html` at that app root.\n"
                 "- If you add/enable ESLint, include an ESLint config and required TS parser/plugins.\n"
                 "- NPM scripts must be Windows-compatible: avoid single quotes around globs; prefer double quotes.\n"
                 "- For env vars in scripts (e.g. NODE_ENV=production), prefer `cross-env` for cross-platform.\n"
                 "\n"
                 "- Delivery-first: if the task implies \"real-time\"/\"price\"/\"live data\", implement a configurable real data source when feasible; "
                 "otherwise fall back to mock BUT label it clearly (e.g. `source=mock`) and document how to switch to real data in README.\n"
                 "- Never claim \"real\" data if it's mock; keep the UI/API honest.\n"
                "\n\n"
                f"{workflow_hint}"
            ),
            user=coder_user,
        )
        change, _change_meta = primary_coder.chat_json(schema=packs.CodeChange, messages=coder_msgs, user=coder_user)
        change, write_pointers = self._materialize_code_change_with_repair(
            change=change,
            actor_agent_id=primary_coder_id,
            actor=primary_coder,
            actor_role=coder_role,
            workflow_hint=workflow_hint,
        )

        self._append_guarded(
            event=new_event(
                agent=primary_coder_id,
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
        mock_mode = os.getenv("VIBE_MOCK_MODE", "").strip() == "1"
        qa_commands = self._determine_test_commands(profile=qa_profile)

        envspec_ptr: Optional[str] = None
        envspec_commands: list[str] = []

        def maybe_envspec(*, reason: str, event_type: str) -> tuple[Optional[str], list[str]]:
            nonlocal envspec_ptr, envspec_commands
            if envspec_ptr is not None:
                return envspec_ptr, envspec_commands
            if env_engineer is None:
                return None, []
            try:
                env_user = (
                    f"Task:\n{task_text}\n\n"
                    f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
                    f"Plan:\n{plan.model_dump_json()}\n\n"
                    f"ContextPacket:\n{ctx.model_dump_json()}\n\n"
                    "Problem:\n"
                    f"- QA profile is {qa_profile}\n"
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
                raise RuntimeError("env_engineer must be activated for L3+ routes")
            maybe_envspec(reason="L3+ env gate", event_type="ENV_UPDATED")

        # On-demand env probing (L1/L2): if we can't find any runnable QA commands,
        # ask env_engineer to propose a minimal runnable command set.
        if (not mock_mode) and (not qa_commands) and env_engineer is not None:
            _ptr, cmds = maybe_envspec(reason="No QA commands detected by heuristics", event_type="ENV_PROBED")
            if cmds:
                qa_commands = cmds

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
                raise RuntimeError("code_reviewer must be activated for L2+ routes")

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
            if not security:
                raise RuntimeError("security must be activated for L3+ routes")
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
            sec_msgs = self._messages_with_memory(
                agent_id="security",
                system=(
                    "你是安全审计（security）。你必须只列出 blockers 和 high 风险，其他不要展开。\n"
                    "只输出 JSON（不要 markdown），并严格匹配 RiskRegister schema："
                    "{passed: bool, blockers: RiskItem[], highs: RiskItem[]}。\n"
                    "不要额外 key；不要最外层包一层对象。\n\n"
                    "规则：\n"
                    "- passed 只有在 blockers 和 highs 都为空时才为 true。\n"
                    "- 每个 RiskItem 必须给出可定位的 pointers（文件片段/日志/契约）。\n\n"
                    f"{workflow_hint}"
                ),
                user=sec_user,
            )
            reg, _ = security.chat_json(schema=packs.RiskRegister, messages=sec_msgs, user=sec_user)
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
            if not compliance:
                raise RuntimeError("compliance must be activated for L4 routes")
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
            if not performance:
                raise RuntimeError("performance must be activated for L4 routes")
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
            max_loops = int(getattr(self.config.behavior, "fix_loop_max_loops", 3) or 3)
            # New scaffolds commonly have multiple independent blockers (build/lint/test across workspaces).
            # Keep bounded, but allow more retries for higher routes / multiple failing commands.
            if route_level in {"L3", "L4"}:
                max_loops = max(max_loops, 6)
            try:
                n_blockers = len([b for b in (report.blockers or []) if str(b).strip()])
                if n_blockers > 1:
                    max_loops = max(max_loops, min(12, 2 + n_blockers))
            except Exception:
                pass
            max_loops = max(1, min(max_loops, 12))
            loop = 0
            fix_history: list[str] = []
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

                if blocker_source == "review":
                    fix_coder_id = self._select_fix_coder_for_review(review=review, activated_agents=activated_agents)
                elif blocker_source == "tests":
                    fix_coder_id = self._select_fix_coder_for_tests(
                        report=report, blocker_text=blocker_text, activated_agents=activated_agents
                    )
                else:
                    fix_coder_id = self._select_fix_coder_for_text(text=blocker_text, activated_agents=activated_agents)

                if fix_coder_id == "coder_frontend" and coder_frontend is None:
                    fix_coder_id = "coder_backend"
                if fix_coder_id == "integration_engineer" and integrator is None:
                    fix_coder_id = "coder_backend"

                fix_coder = (
                    coder_frontend
                    if fix_coder_id == "coder_frontend" and coder_frontend is not None
                    else integrator
                    if fix_coder_id == "integration_engineer" and integrator is not None
                    else coder_backend
                )

                # Audit: record which agent is handling this fix-loop step.
                self._append_guarded(
                    event=new_event(
                        agent="router",
                        type="STATE_TRANSITION",
                        summary=f"Fix-loop {loop}: dispatch to {fix_coder_id}",
                        branch_id=self.branch_id,
                        pointers=[],
                        meta={
                            "phase": "fix_loop",
                            "loop": loop,
                            "blocker_source": blocker_source,
                            "fix_agent": fix_coder_id,
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
                if ctx_excerpts:
                    fix_user = f"{fix_user}\n\nRepoExcerpts:\n{ctx_excerpts}"
                if blocker_source == "tests":
                    failure_excerpts = self._repo_excerpts_for_test_failure(report)
                    if failure_excerpts:
                        fix_user = f"{fix_user}\n\nFailureRepoExcerpts:\n{failure_excerpts}"

                if fix_history:
                    fix_user = f"{fix_user}\n\nFixLoopHistory（已尝试的修复，避免重复）：\n" + "\n".join(fix_history[-6:])

                if blocker_source == "tests":
                    hint = self._fix_loop_autohint_for_tests(report=report, blocker_text=blocker_text)
                    if hint:
                        fix_user = f"{fix_user}\n\nAutoHint（硬逻辑，仅供参考，事实以 pointers 展开为准）：\n{hint}"

                auto_change: Optional[packs.CodeChange] = None
                if blocker_source == "tests":
                    auto_change = self._auto_code_change_for_test_failure(report=report, blocker_text=blocker_text)

                fix_role = "Coder"
                if fix_coder_id == "coder_frontend":
                    fix_role = "Frontend Coder (React/TypeScript)"
                elif fix_coder_id == "integration_engineer":
                    fix_role = "Integration Engineer (align frontend/backend/contracts)"
                elif fix_coder_id == "coder_backend":
                    fix_role = "Backend Coder"
                if auto_change is None:
                    fix_msgs = self._messages_with_memory(
                        agent_id=fix_coder_id,
                        system=(
                            f"You are {fix_role}. Fix exactly one blocker. Return JSON only for CodeChange with fields: "
                            "kind ('commit'|'patch'|'noop'), summary, writes? (list[{path,content}]), commit_hash?, patch_pointer?, files_changed[], blockers[]. "
                            "Prefer 'writes' for file changes. No extra keys. No markdown.\n\n"
                            "Hard rules:\n"
                            "- Never write under `.vibe/` or `.git/` (those are internal system dirs).\n"
                            "- Use only repo-root relative paths (no absolute paths / drive letters).\n"
                        "- Fix the failing command in the blocker (it may require multiple file edits, but it is ONE blocker).\n"
                        "- Do not do architecture refactors during fix-loop.\n"
                            "- If you add/modify an import, ensure the target file exists or create it in writes.\n\n"
                            "- If you import a new npm package, add it to the correct `package.json` (dependencies/devDependencies).\n\n"
                            "- NPM scripts must be Windows-compatible: avoid single quotes around globs; prefer double quotes.\n"
                            "- For env vars in scripts (e.g. NODE_ENV=production), prefer `cross-env` for cross-platform.\n\n"
                            f"{workflow_hint}"
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
                    )
                else:
                    change, write_pointers = self._materialize_code_change(auto_change, actor_agent_id=fix_coder_id)
                try:
                    evidence = change.commit_hash or change.patch_pointer or ""
                    files = [str(x).strip() for x in (change.files_changed or []) if str(x).strip()]
                    files_short = ", ".join(files[:6]) + (" ..." if len(files) > 6 else "")
                    fix_history.append(f"- loop {loop} {fix_coder_id}: {change.summary.strip()[:160]} | files: {files_short} | evidence: {evidence}")
                except Exception:
                    pass
                meta = {"blocker": blocker_text, "blocker_source": blocker_source, "route_level": route_level, "style": resolved_style}
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

                qa_commands_loop = self._determine_test_commands(profile=qa_profile)
                self._append_guarded(
                    event=new_event(
                        agent="qa",
                        type="TEST_RUN",
                        summary=f"Fix-loop {loop}: re-running tests",
                        branch_id=self.branch_id,
                        pointers=[],
                        meta={"profile": qa_profile, "commands": qa_commands_loop, "route_level": route_level, "style": resolved_style},
                    ),
                    activated_agents=activated_agents,
                )
                report = self._run_tests(profile=qa_profile, commands=qa_commands_loop)
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

                artifacts_blocked: List[str] = []
                artifacts_blocked.extend([p for p in [usecases_ptr, decisions_ptr, contract_ptr, review_ptr, security_ptr, compliance_ptr, perf_ptr, envspec_ptr] if p])
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
                        "route_level": route_level,
                        "agents": activated_agents_list,
                        "qa_profile": qa_profile,
                        "reason": "fix_loop_blockers",
                        "blockers": blockers[:20],
                        "style": resolved_style,
                    },
                )
                self._append_guarded(
                    event=new_event(
                        agent="router",
                        type="CHECKPOINT_CREATED",
                        summary=f"Created checkpoint {cp.id} (non-green, blockers remain)",
                        branch_id=self.branch_id,
                        pointers=artifacts_blocked,
                        meta={
                            "green": False,
                            "repo_ref": repo_ref,
                            "route_level": route_level,
                            "agents": activated_agents_list,
                            "style": resolved_style,
                            "reason": "fix_loop_blockers",
                            "blockers": blockers[:20],
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
            if doc_writer is None:
                raise RuntimeError("doc_writer must be activated for L3+ routes")
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

            if release_manager is None:
                raise RuntimeError("release_manager must be activated for L3+ routes")
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
            if support_engineer is None:
                raise RuntimeError("support_engineer must be activated for L4 routes")
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
                if data_engineer is None:
                    raise RuntimeError("data_engineer must be activated for migration tasks in L4")
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
                "route_level": route_level,
                "requested_route_level": requested_route_level,
                "agents": activated_agents_list,
                "qa_profile": qa_profile,
                "style": resolved_style,
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
