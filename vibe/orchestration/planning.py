from __future__ import annotations

from typing import Optional

from vibe.orchestration.shared import _normalize_scope_pattern
from vibe.orchestration.work_orders import ExecutionWorkOrder, fix_loop_scope, plan_task_work_order
from vibe.schemas import packs


class PlanningRuntimeMixin:
    def _sanitize_implementation_blueprint(
        self,
        bp: packs.ImplementationBlueprint,
        *,
        lead_fix_agents: set[str],
        lead_consult_advisors: set[str],
        lead_fix_order_owners: set[str],
    ) -> packs.ImplementationBlueprint:
        def clean(pats: list[str], *, limit: int) -> list[str]:
            out: list[str] = []
            seen: set[str] = set()
            for raw in list(pats or [])[: max(0, limit * 3)]:
                s = _normalize_scope_pattern(str(raw or ""))
                if not s:
                    continue
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
        for scope in scopes[:64]:
            try:
                task_id = str(getattr(scope, "task_id", "") or "").strip()
            except Exception:
                task_id = ""
            if not task_id:
                continue
            allow = clean(list(getattr(scope, "allowed_write_globs", []) or []), limit=24)
            deny = clean(list(getattr(scope, "denied_write_globs", []) or []), limit=24)
            notes = str(getattr(scope, "notes", "") or "").strip()
            norm_scopes.append(
                packs.ImplementationBlueprintTaskScope(
                    task_id=task_id,
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

        work_orders: list[packs.FixWorkOrder] = []
        try:
            raw_orders = list(getattr(bp, "fix_work_orders", []) or [])
        except Exception:
            raw_orders = []
        for raw in raw_orders[:8]:
            try:
                order = raw if isinstance(raw, packs.FixWorkOrder) else packs.FixWorkOrder.model_validate(raw)
            except Exception:
                continue
            owner = str(getattr(order, "owner", "") or "").strip()
            if owner not in lead_fix_order_owners:
                continue
            allow = clean(list(getattr(order, "allowed_write_globs", []) or []), limit=16)
            deny = clean(list(getattr(order, "denied_write_globs", []) or []), limit=16)
            files_to_check = [str(x).strip() for x in list(getattr(order, "files_to_check", []) or []) if str(x).strip()][:12]
            commands = [str(x).strip() for x in list(getattr(order, "commands", []) or []) if str(x).strip()][:8]
            verify = [str(x).strip() for x in list(getattr(order, "verify_commands", []) or []) if str(x).strip()][:8]
            stop_if = [str(x).strip() for x in list(getattr(order, "stop_if", []) or []) if str(x).strip()][:8]
            pointers = [str(x).strip() for x in list(getattr(order, "pointers", []) or []) if str(x).strip()][:12]
            summary = str(getattr(order, "summary", "") or "").strip()[:240]
            if not summary:
                continue
            reason = str(getattr(order, "reason", "") or "").strip()[:240]
            work_orders.append(
                packs.FixWorkOrder(
                    owner=owner,
                    summary=summary,
                    reason=reason,
                    allowed_write_globs=allow,
                    denied_write_globs=deny,
                    files_to_check=files_to_check,
                    commands=commands,
                    verify_commands=verify,
                    stop_if=stop_if,
                    pointers=pointers,
                )
            )
        bp.fix_work_orders = work_orders
        bp.escalation_reason = str(getattr(bp, "escalation_reason", "") or "").strip()[:240]
        bp.invariants = [str(x).strip()[:240] for x in list(bp.invariants or []) if str(x).strip()][:16]
        bp.verification = [str(x).strip()[:240] for x in list(bp.verification or []) if str(x).strip()][:12]
        bp.pointers = [str(x).strip() for x in list(bp.pointers or []) if str(x).strip()][:24]
        return bp

    def _plan_work_order_from_blueprint(
        self,
        *,
        blueprint: Optional[packs.ImplementationBlueprint],
        task: packs.PlanTask,
    ) -> ExecutionWorkOrder:
        return plan_task_work_order(task, blueprint=blueprint)

    def _fix_loop_scope_from_blueprint(
        self,
        *,
        blueprint: Optional[packs.ImplementationBlueprint],
    ) -> tuple[list[str], list[str]]:
        return fix_loop_scope(blueprint)

    def _preferred_fix_agent_from_blueprint(
        self,
        *,
        blueprint: Optional[packs.ImplementationBlueprint],
        current_fix_agent: str,
        lead_fix_agents: set[str],
    ) -> str:
        preferred = str(getattr(blueprint, "recommended_fix_agent", "") or "").strip() if blueprint is not None else ""
        if preferred in lead_fix_agents and preferred in self.config.agents:
            return preferred
        return current_fix_agent

    def _plan_task_system_prompt(
        self,
        *,
        role: str,
        task: packs.PlanTask,
        work_order: ExecutionWorkOrder,
        workflow_hint: str,
    ) -> str:
        scope_lines: list[str] = []
        allow = [str(x).strip() for x in list(work_order.allowed_write_globs or []) if str(x).strip()][:12]
        deny = [str(x).strip() for x in list(work_order.denied_write_globs or []) if str(x).strip()][:12]
        inv = [str(x).strip() for x in list(work_order.invariants or []) if str(x).strip()][:8]
        ver = [str(x).strip() for x in list(work_order.verification_targets or []) if str(x).strip()][:6]
        notes = str(getattr(work_order, "notes", "") or "").strip()
        if allow:
            scope_lines.append("Allowed write paths/globs (MUST stay within):\n" + "\n".join([f"- {p}" for p in allow]))
        if deny:
            scope_lines.append("Denied write paths/globs:\n" + "\n".join([f"- {p}" for p in deny]))
        if notes:
            scope_lines.append(f"Task scope notes:\n- {notes}")
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
            "- Do not add/modify tests unless this PlanTask explicitly mentions tests/QA/验证（否则先把工程与核心功能跑通，再由后续任务补测试）。\n"
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

    def _plan_task_user_prompt(
        self,
        *,
        task_text: str,
        req: Optional[packs.RequirementPack],
        intent: Optional[packs.IntentExpansionPack],
        decisions: packs.DecisionPack,
        contract: packs.ContractPack,
        task: packs.PlanTask,
        work_order: ExecutionWorkOrder,
        plan: packs.Plan,
        ctx: packs.ContextPacket,
        usecases: Optional[packs.UseCasePack],
        ctx_excerpts: str,
    ) -> str:
        base = (
            f"Task:\n{task_text}\n\n"
            f"RequirementPack:\n{req.model_dump_json() if req is not None else '{}'}\n\n"
            f"IntentExpansionPack:\n{intent.model_dump_json() if intent is not None else '{}'}\n\n"
            f"DecisionPack:\n{decisions.model_dump_json() if decisions is not None else '{}'}\n\n"
            f"ContractPack:\n{contract.model_dump_json() if contract is not None else '{}'}\n\n"
            f"PlanTask:\n{task.model_dump_json()}\n\n"
            f"ExecutionWorkOrder:\n{work_order.model_dump_json()}\n\n"
            f"FullPlan:\n{plan.model_dump_json()}\n\n"
            f"ContextPacket:\n{ctx.model_dump_json()}"
        )
        if usecases is not None:
            base = f"{base}\n\nUseCasePack:\n{usecases.model_dump_json()}"
        if ctx_excerpts:
            base = f"{base}\n\nRepoExcerpts:\n{ctx_excerpts}"
        return base
