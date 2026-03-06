from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from vibe.cli import app
from vibe.config import VibeConfig, write_default_config
from vibe.schemas import packs as schemas
from vibe.storage.artifacts import ArtifactsStore
from vibe.storage.checkpoints import CheckpointsStore
from vibe.storage.ledger import Ledger


def _force_mock_providers(repo_root: Path) -> None:
    cfg_path = repo_root / ".vibe" / "vibe.yaml"
    cfg = VibeConfig.load(cfg_path)
    # Mock LLMs but keep QA real (do NOT set VIBE_MOCK_MODE).
    for agent_id, a in cfg.agents.items():
        if a.enabled:
            a.provider = "mock"
            a.model = "mock"
    cfg.behavior.fix_loop_max_loops = 1
    write_default_config(repo_root, cfg)


def test_cli_run_resume_skips_spec_and_plan(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VIBE_MOCK_MODE", raising=False)

    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output
    _force_mock_providers(tmp_path)

    # Create a real failing repo (pytest).
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"resume-test\"\nversion = \"0.0.0\"\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_fail.py").write_text(
        "def test_fail():\n    assert False\n",
        encoding="utf-8",
    )

    r2 = runner.invoke(app, ["task", "add", "make tests pass"])
    assert r2.exit_code == 0, r2.output

    # First run: should fail and create a non-green checkpoint.
    r3 = runner.invoke(app, ["run", "--route", "L1"])
    assert r3.exit_code == 0, r3.output
    ckpt1 = r3.output.strip()

    cps = CheckpointsStore(tmp_path)
    cp1 = cps.get(ckpt1)
    assert cp1.green is False
    assert cp1.meta.get("reason") == "fix_loop_blockers"
    assert cp1.meta.get("task_id")

    ledger = Ledger(tmp_path)
    ac_count_1 = sum(1 for _ in ledger.iter_events(types={"AC_DEFINED"}))
    plan_count_1 = sum(1 for _ in ledger.iter_events(types={"PLAN_CREATED"}))
    implement_count_1 = sum(
        1
        for e in ledger.iter_events(types={"STATE_TRANSITION"})
        if (e.meta or {}).get("phase") == "implement"
    )
    assert ac_count_1 == 1
    assert plan_count_1 == 1
    assert implement_count_1 >= 1

    # Second run (default resume=True): should continue from the non-green checkpoint
    # and MUST NOT recreate spec/plan/implement.
    r4 = runner.invoke(app, ["run", "--route", "L1"])
    assert r4.exit_code == 0, r4.output
    ckpt2 = r4.output.strip()

    cp2 = cps.get(ckpt2)
    assert cp2.green is False
    assert cp2.meta.get("resume_from") == ckpt1

    ac_count_2 = sum(1 for _ in ledger.iter_events(types={"AC_DEFINED"}))
    plan_count_2 = sum(1 for _ in ledger.iter_events(types={"PLAN_CREATED"}))
    implement_count_2 = sum(
        1
        for e in ledger.iter_events(types={"STATE_TRANSITION"})
        if (e.meta or {}).get("phase") == "implement"
    )
    assert ac_count_2 == 1
    assert plan_count_2 == 1
    assert implement_count_2 == implement_count_1


def test_cli_run_resume_replan_checkpoint_reenters_implement(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    r1 = runner.invoke(app, ["init"])
    assert r1.exit_code == 0, r1.output

    r2 = runner.invoke(app, ["task", "add", "continue from replanned checkpoint"])
    assert r2.exit_code == 0, r2.output
    task_id = r2.output.strip()

    artifacts = ArtifactsStore(tmp_path)
    req = schemas.RequirementPack(
        summary="resume replan test",
        acceptance=["mock: implementation resumes from replanned checkpoint"],
        non_goals=[],
        constraints=[],
    )
    plan = schemas.Plan(
        tasks=[
            schemas.PlanTask(
                id="t_impl",
                title="Implement from replanned checkpoint",
                agent="coder_backend",
                description="Write the minimal scaffold from the stored plan.",
            )
        ]
    )
    blueprint = schemas.ImplementationBlueprint(
        summary="stored implementation blueprint",
        global_allowed_write_globs=["**"],
        global_denied_write_globs=[".vibe/**", ".git/**"],
        task_scopes=[],
        fix_allowed_write_globs=[],
        fix_denied_write_globs=[],
        recommended_fix_agent="coder_backend",
        consult_agents=[],
        escalation_reason="resume from replanned checkpoint",
        invariants=[],
        verification=[],
        pointers=[],
    )
    req_ptr = artifacts.put_json(req.model_dump(), suffix=".req.json", kind="req").to_pointer()
    plan_ptr = artifacts.put_json(plan.model_dump(), suffix=".plan.json", kind="plan").to_pointer()
    blueprint_ptr = artifacts.put_json(
        blueprint.model_dump(),
        suffix=".impl_blueprint.json",
        kind="impl_blueprint",
    ).to_pointer()

    cps = CheckpointsStore(tmp_path)
    seed_cp = cps.create(
        checkpoint_id="ckpt_replan_seed",
        label="resume replan test",
        repo_ref="no-git",
        ledger_offset=0,
        artifacts=[req_ptr, plan_ptr, blueprint_ptr],
        green=False,
        restore_steps=["vibe run"],
        meta={
            "branch_id": "main",
            "task_id": task_id,
            "route_level": "L2",
            "requested_route_level": "L2",
            "agents": ["router", "pm", "coder_backend"],
            "reason": "replan_required",
            "blockers": ["architecture replanned after blockers"],
            "req_ptr": req_ptr,
            "plan_ptr": plan_ptr,
            "impl_blueprint_ptr": blueprint_ptr,
        },
    )

    r3 = runner.invoke(app, ["run", "--mock", "--mock-writes", "--route", "L2"])
    assert r3.exit_code == 0, r3.output
    ckpt_id = r3.output.strip()

    cp = cps.get(ckpt_id)
    assert cp.meta.get("resume_from") == seed_cp.id
    assert (tmp_path / "README.md").exists()

    ledger = Ledger(tmp_path)
    implement_events = [
        e
        for e in ledger.iter_events(types={"STATE_TRANSITION"})
        if (e.meta or {}).get("phase") == "implement"
    ]
    assert implement_events
    assert sum(1 for _ in ledger.iter_events(types={"PLAN_CREATED"})) == 0
