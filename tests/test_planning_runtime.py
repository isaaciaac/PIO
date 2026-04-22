from __future__ import annotations

from vibe.orchestration.planning import PlanningRuntimeMixin
from vibe.orchestration.work_orders import ExecutionWorkOrder
from vibe.schemas import packs


class _DummyPlanningRuntime(PlanningRuntimeMixin):
    pass


def test_sanitize_implementation_blueprint_removes_unsafe_paths_and_unknown_owners() -> None:
    runtime = _DummyPlanningRuntime()
    blueprint = packs.ImplementationBlueprint.model_validate(
        {
            "summary": "lead blueprint",
            "allow": [".vibe/**", "src/**", "C:/tmp/bad"],
            "deny": [".git/**", "dist/**"],
            "consult": ["architect", "bogus"],
            "recommended_agent": "integration_engineer",
            "work_orders": [
                {
                    "agent": "integration_engineer",
                    "title": "repair boundary",
                    "allow": ["src/contracts/**", ".git/**"],
                },
            ],
        }
    )

    cleaned = runtime._sanitize_implementation_blueprint(
        blueprint,
        lead_fix_agents={"coder_backend", "coder_frontend", "integration_engineer"},
        lead_consult_advisors={"architect", "env_engineer", "api_confirm", "ops_engineer"},
        lead_fix_order_owners={"coder_backend", "coder_frontend", "integration_engineer", "env_engineer", "ops_engineer"},
    )

    assert cleaned.global_allowed_write_globs == ["src/**"]
    assert cleaned.global_denied_write_globs == ["dist/**"]
    assert cleaned.consult_agents == ["architect"]
    assert cleaned.recommended_fix_agent == "integration_engineer"
    assert len(cleaned.fix_work_orders) == 1
    assert cleaned.fix_work_orders[0].owner == "integration_engineer"
    assert cleaned.fix_work_orders[0].allowed_write_globs == ["src/contracts/**"]


def test_plan_task_prompt_builders_include_execution_work_order_contract() -> None:
    runtime = _DummyPlanningRuntime()
    task = packs.PlanTask(id="T-42", title="Ship endpoint", agent="coder_backend", description="Implement endpoint only")
    work_order = ExecutionWorkOrder(
        id="plan:T-42",
        kind="plan_task",
        owner="coder_backend",
        summary="Ship endpoint",
        reason="Implement endpoint only",
        allowed_write_globs=["src/api/**"],
        denied_write_globs=["src/api/generated/**"],
        notes="Touch the API slice only.",
        invariants=["Keep response schema stable."],
        verification_targets=["pytest tests/api -q"],
    )
    system_prompt = runtime._plan_task_system_prompt(
        role="Backend Coder",
        task=task,
        work_order=work_order,
        workflow_hint="WorkflowHint",
    )
    user_prompt = runtime._plan_task_user_prompt(
        task_text="Build endpoint",
        req=packs.RequirementPack(summary="Build endpoint"),
        intent=None,
        decisions=packs.DecisionPack(),
        contract=packs.ContractPack(),
        task=task,
        work_order=work_order,
        plan=packs.Plan(tasks=[task]),
        ctx=packs.ContextPacket(),
        usecases=None,
        ctx_excerpts="src/api/routes.py",
    )

    assert "Allowed write paths/globs" in system_prompt
    assert "Task scope notes" in system_prompt
    assert "WorkflowHint" in system_prompt
    assert "ExecutionWorkOrder" in user_prompt
    assert "plan:T-42" in user_prompt
    assert "src/api/routes.py" in user_prompt
