from __future__ import annotations

from vibe.delivery import augment_plan, augment_requirement_pack, infer_delivery_needs
from vibe.schemas import packs


def test_infer_delivery_needs_detects_live_data() -> None:
    needs = infer_delivery_needs("帮我做一个黄金价格的实时呈现面板")
    assert needs.wants_live_data is True


def test_augment_requirement_pack_adds_delivery_acceptance_and_live_data_rules() -> None:
    req = packs.RequirementPack(summary="x", acceptance=[], non_goals=[], constraints=[])
    out = augment_requirement_pack(req, task_text="做一个实时黄金价格面板")
    assert any("README.md" in x for x in out.acceptance)
    assert any("数据" in x or "mock" in x.lower() for x in out.acceptance)
    assert any(x.startswith("Assume:") for x in out.constraints)


def test_augment_plan_inserts_delivery_tasks_when_missing() -> None:
    plan = packs.Plan(tasks=[packs.PlanTask(id="t1", title="实现", agent="coder_backend", description="写代码")])
    out = augment_plan(plan, req=None, task_text="做一个实时黄金价格面板", activated_agents={"pm", "router", "coder_backend", "qa"})
    assert len(out.tasks) <= 5
    assert any("交付" in t.title or "README" in (t.description or "") for t in out.tasks)
    assert any("数据源" in t.title or "数据源" in (t.description or "") for t in out.tasks)

