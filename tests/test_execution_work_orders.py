from __future__ import annotations

from vibe.orchestration.work_orders import (
    fix_candidate_work_order,
    fix_loop_scope,
    plan_task_work_order,
    resolved_work_order,
)
from vibe.schemas import packs


def test_plan_task_work_order_uses_blueprint_scope_and_guidance() -> None:
    task = packs.PlanTask(id="T-1", title="Implement API handler", agent="coder_backend", description="Build the first API slice")
    blueprint = packs.ImplementationBlueprint.model_validate(
        {
            "summary": "lead blueprint",
            "allow": ["src/**"],
            "deny": ["secrets/**"],
            "tasks": [
                {
                    "task_id": "T-1",
                    "allow": ["src/api/**"],
                    "deny": ["src/api/generated/**"],
                    "notes": "Only touch API surface files for this task.",
                }
            ],
            "invariants": ["Keep API payloads backward compatible."],
            "verification": ["pytest tests/api -q"],
        }
    )

    order = plan_task_work_order(task, blueprint=blueprint)

    assert order.id == "plan:T-1"
    assert order.kind == "plan_task"
    assert order.allowed_write_globs == ["src/api/**"]
    assert order.denied_write_globs == ["secrets/**", "src/api/generated/**"]
    assert order.notes == "Only touch API surface files for this task."
    assert order.invariants == ["Keep API payloads backward compatible."]
    assert order.verification_targets == ["pytest tests/api -q"]


def test_fix_loop_scope_prefers_fix_scope_and_merges_global_denies() -> None:
    blueprint = packs.ImplementationBlueprint.model_validate(
        {
            "summary": "lead blueprint",
            "allow": ["src/**"],
            "deny": ["vendor/**"],
            "fix_allow": ["src/payments/**"],
            "fix_deny": ["src/payments/legacy/**"],
        }
    )

    allow, deny = fix_loop_scope(blueprint)

    assert allow == ["src/payments/**"]
    assert deny == ["vendor/**", "src/payments/legacy/**"]


def test_fix_candidate_and_resolved_work_order_keep_execution_boundary_explicit() -> None:
    raw = packs.FixWorkOrder.model_validate(
        {
            "owner": "integration_engineer",
            "summary": "Repair contract drift",
            "reason": "Frontend and backend disagree on payload shape.",
            "allow": ["src/frontend/**", "src/backend/**"],
            "deny": ["src/**", "src/backend/generated/**"],
            "files": ["src/frontend/api.ts", "src/backend/contracts.py"],
            "commands": ["pytest tests/contracts -q"],
            "verify": ["pytest tests/integration -q"],
            "stop_if": ["Scope denied repeats"],
            "pointers": ["artifacts/fix.json@sha256:abc"],
        }
    )

    candidate = fix_candidate_work_order(raw)
    resolved = resolved_work_order(
        candidate,
        allowed_write_globs=["src/frontend/**", "src/backend/**", "tests/contracts/**"],
        denied_write_globs=["src/backend/generated/**"],
        notes="Repair arena level: L2",
    )

    assert candidate.kind == "fix_work_order"
    assert candidate.owner == "integration_engineer"
    assert candidate.commands == ["pytest tests/contracts -q"]
    assert candidate.verify_commands == ["pytest tests/integration -q"]
    assert resolved.allowed_write_globs == ["src/frontend/**", "src/backend/**", "tests/contracts/**"]
    assert resolved.denied_write_globs == ["src/backend/generated/**"]
    assert resolved.notes == "Repair arena level: L2"
