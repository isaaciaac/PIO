from __future__ import annotations

import hashlib
from typing import Literal, Optional

from pydantic import BaseModel, Field

from vibe.orchestration.shared import _normalize_scope_pattern
from vibe.schemas import packs


ExecutionWorkOrderKind = Literal["plan_task", "fix_work_order"]


def _clean_scope(values: list[str] | None, *, limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in list(values or []):
        norm = _normalize_scope_pattern(str(raw or ""))
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
        if len(out) >= limit:
            break
    return out


def _clean_text(values: list[str] | None, *, limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in list(values or []):
        item = str(raw or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _merge_scope(*parts: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for part in parts:
        for raw in list(part or []):
            norm = _normalize_scope_pattern(str(raw or ""))
            if not norm or norm in seen:
                continue
            seen.add(norm)
            merged.append(norm)
    return merged


def _fix_work_order_id(*, owner: str, summary: str, reason: str) -> str:
    digest = hashlib.sha1(f"{owner}|{summary}|{reason}".encode("utf-8")).hexdigest()[:12]
    return f"fix:{owner or 'unknown'}:{digest}"


class ExecutionWorkOrder(BaseModel):
    id: str
    kind: ExecutionWorkOrderKind
    owner: str
    summary: str
    reason: str = ""
    source_task_id: str = ""
    source_task_title: str = ""
    allowed_write_globs: list[str] = Field(default_factory=list)
    denied_write_globs: list[str] = Field(default_factory=list)
    files_to_check: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    verify_commands: list[str] = Field(default_factory=list)
    stop_if: list[str] = Field(default_factory=list)
    pointers: list[str] = Field(default_factory=list)
    notes: str = ""
    invariants: list[str] = Field(default_factory=list)
    verification_targets: list[str] = Field(default_factory=list)


def plan_task_work_order(
    task: packs.PlanTask,
    *,
    blueprint: Optional[packs.ImplementationBlueprint],
) -> ExecutionWorkOrder:
    allow: list[str] = []
    deny: list[str] = []
    notes = ""
    if blueprint is not None:
        task_id = str(getattr(task, "id", "") or "").strip()
        for scope in list(blueprint.task_scopes or [])[:96]:
            if str(getattr(scope, "task_id", "") or "").strip() != task_id:
                continue
            allow = _clean_scope(list(getattr(scope, "allowed_write_globs", []) or []), limit=24)
            deny = _clean_scope(list(getattr(scope, "denied_write_globs", []) or []), limit=24)
            notes = str(getattr(scope, "notes", "") or "").strip()
            break
        if not allow:
            allow = _clean_scope(list(blueprint.global_allowed_write_globs or []), limit=48)
        deny = _merge_scope(list(blueprint.global_denied_write_globs or []), deny)
    return ExecutionWorkOrder(
        id=f"plan:{str(getattr(task, 'id', '') or '').strip() or 'task'}",
        kind="plan_task",
        owner=str(getattr(task, "agent", "") or "").strip(),
        summary=str(getattr(task, "title", "") or "").strip(),
        reason=str(getattr(task, "description", "") or "").strip(),
        source_task_id=str(getattr(task, "id", "") or "").strip(),
        source_task_title=str(getattr(task, "title", "") or "").strip(),
        allowed_write_globs=allow,
        denied_write_globs=deny,
        notes=notes,
        invariants=_clean_text(list(getattr(blueprint, "invariants", []) or []), limit=16) if blueprint is not None else [],
        verification_targets=_clean_text(list(getattr(blueprint, "verification", []) or []), limit=12)
        if blueprint is not None
        else [],
    )


def fix_loop_scope(blueprint: Optional[packs.ImplementationBlueprint]) -> tuple[list[str], list[str]]:
    if blueprint is None:
        return [], []
    allow = _clean_scope(list(blueprint.fix_allowed_write_globs or []), limit=48)
    if not allow:
        allow = _clean_scope(list(blueprint.global_allowed_write_globs or []), limit=48)
    deny = _merge_scope(list(blueprint.global_denied_write_globs or []), list(blueprint.fix_denied_write_globs or []))
    return allow, deny


def fix_candidate_work_order(
    order: packs.FixWorkOrder,
) -> ExecutionWorkOrder:
    owner = str(getattr(order, "owner", "") or "").strip()
    summary = str(getattr(order, "summary", "") or "").strip()
    reason = str(getattr(order, "reason", "") or "").strip()
    return ExecutionWorkOrder(
        id=_fix_work_order_id(owner=owner, summary=summary, reason=reason),
        kind="fix_work_order",
        owner=owner,
        summary=summary,
        reason=reason,
        allowed_write_globs=_clean_scope(list(getattr(order, "allowed_write_globs", []) or []), limit=32),
        denied_write_globs=_clean_scope(list(getattr(order, "denied_write_globs", []) or []), limit=32),
        files_to_check=_clean_text(list(getattr(order, "files_to_check", []) or []), limit=16),
        commands=_clean_text(list(getattr(order, "commands", []) or []), limit=8),
        verify_commands=_clean_text(list(getattr(order, "verify_commands", []) or []), limit=8),
        stop_if=_clean_text(list(getattr(order, "stop_if", []) or []), limit=8),
        pointers=_clean_text(list(getattr(order, "pointers", []) or []), limit=16),
    )


def resolved_work_order(
    order: ExecutionWorkOrder,
    *,
    allowed_write_globs: list[str] | None = None,
    denied_write_globs: list[str] | None = None,
    notes: Optional[str] = None,
) -> ExecutionWorkOrder:
    update: dict[str, object] = {}
    if allowed_write_globs is not None:
        update["allowed_write_globs"] = _clean_scope(list(allowed_write_globs or []), limit=64)
    if denied_write_globs is not None:
        update["denied_write_globs"] = _clean_scope(list(denied_write_globs or []), limit=64)
    if notes is not None:
        update["notes"] = str(notes or "").strip()
    return order.model_copy(update=update)
