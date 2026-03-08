from __future__ import annotations

from vibe.delivery import augment_plan
from vibe.schemas import packs


def test_augment_plan_orders_bootstrap_before_tests_and_docs() -> None:
    plan = packs.Plan(
        tasks=[
            packs.PlanTask(id="t_docs", title="交付说明", agent="coder_backend", description="更新 README.md"),
            packs.PlanTask(id="t_tests", title="测试补齐", agent="coder_backend", description="添加 pytest 测试"),
            packs.PlanTask(id="t_core", title="实现核心功能", agent="coder_backend", description="实现 API"),
            packs.PlanTask(id="t_boot", title="工程骨架", agent="coder_backend", description="补齐 pyproject/requirements/入口"),
        ]
    )
    out = augment_plan(plan, req=None, task_text="从0做个小服务", activated_agents={"coder_backend"}, max_tasks=10)
    ids = [t.id for t in out.tasks]
    assert ids.index("t_boot") < ids.index("t_core")
    assert ids.index("t_core") < ids.index("t_tests")
    assert ids.index("t_tests") < ids.index("t_docs")

