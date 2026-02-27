from __future__ import annotations

import os
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

import typer

from vibe.config import VibeConfig, default_config, write_default_config
from vibe.manifests import write_manifests
from vibe.orchestrator import Orchestrator
from vibe.repo import ensure_vibe_dirs, find_repo_root
from vibe.storage.checkpoints import CheckpointsStore
from vibe.schemas.events import new_event
from vibe.storage.ledger import Ledger
from vibe.tools.git import GitTool
from vibe.branching import detect_branch_id

app = typer.Typer(help="vibe coding / multi-agent orchestrator (MVP)")
config_app = typer.Typer(help="Config commands")
task_app = typer.Typer(help="Task commands")
checkpoint_app = typer.Typer(help="Checkpoint commands")
branch_app = typer.Typer(help="Branch commands")

app.add_typer(config_app, name="config")
app.add_typer(task_app, name="task")
app.add_typer(checkpoint_app, name="checkpoint")
app.add_typer(branch_app, name="branch")


@app.command()
def init(path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)")) -> None:
    repo_root = find_repo_root(path or Path.cwd())
    cfg = default_config()
    ensure_vibe_dirs(repo_root, agent_ids=list(cfg.agents.keys()))
    write_default_config(repo_root, cfg)
    write_manifests(repo_root)
    typer.echo(f"Initialized .vibe in {repo_root}")


@config_app.command("show")
def config_show(path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)")) -> None:
    repo_root = find_repo_root(path or Path.cwd())
    cfg_path = repo_root / ".vibe" / "vibe.yaml"
    if not cfg_path.exists():
        raise typer.Exit(code=2)
    cfg = VibeConfig.load(cfg_path)
    typer.echo(cfg.redacted().model_dump_json(indent=2))


@task_app.command("add")
def task_add(
    text: str = typer.Argument(..., help="Task description"),
    path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)"),
) -> None:
    repo_root = find_repo_root(path or Path.cwd())
    if not (repo_root / ".vibe" / "ledger.jsonl").exists():
        raise typer.Exit(code=2)
    git = GitTool(repo_root)
    branch_id = detect_branch_id(repo_root, git=git)
    ledger = Ledger(repo_root, branch_id=branch_id)
    event = new_event(
        agent="user",
        type="REQ_CREATED",
        summary=text.strip().splitlines()[0][:200],
        branch_id=branch_id,
        meta={"text": text},
    )
    ledger.append(event)
    typer.echo(event.id)


@app.command()
def run(
    task: Optional[str] = typer.Option(None, "--task", help="Task event id (default: latest)"),
    path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)"),
    mock: bool = typer.Option(False, "--mock", help="Force mock mode for this run"),
) -> None:
    if mock:
        os.environ["VIBE_MOCK_MODE"] = "1"
    repo_root = find_repo_root(path or Path.cwd())
    orch = Orchestrator(repo_root)
    result = orch.run(task_id=task)
    typer.echo(result.checkpoint_id)


@checkpoint_app.command("list")
def checkpoint_list(path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)")) -> None:
    repo_root = find_repo_root(path or Path.cwd())
    store = CheckpointsStore(repo_root)
    typer.echo(store.load().model_dump_json(indent=2))


@checkpoint_app.command("create")
def checkpoint_create(
    label: str = typer.Option("manual", "--label", help="Checkpoint label"),
    green: bool = typer.Option(False, "--green", help="Mark checkpoint as green"),
    path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)"),
) -> None:
    repo_root = find_repo_root(path or Path.cwd())
    git = GitTool(repo_root)
    branch_id = detect_branch_id(repo_root, git=git)
    ledger = Ledger(repo_root, branch_id=branch_id)
    store = CheckpointsStore(repo_root)
    repo_ref = git.head_sha() if git.is_repo() else "no-git"
    artifacts = []
    if repo_ref == "no-git":
        snap = store.snapshot_repo()
        artifacts.append(snap.to_pointer())
    checkpoint_id = f"ckpt_{uuid4().hex[:12]}"
    cp = store.create(
        checkpoint_id=checkpoint_id,
        label=label,
        repo_ref=repo_ref,
        ledger_offset=ledger.count_lines(),
        artifacts=artifacts,
        green=green,
        restore_steps=[f"git checkout --detach {repo_ref}"] if repo_ref != "no-git" else [f"vibe checkpoint restore {checkpoint_id}"],
    )
    ledger.append(
        new_event(
            agent="router",
            type="CHECKPOINT_CREATED",
            summary=f"Manual checkpoint {cp.id}",
            branch_id=branch_id,
            pointers=artifacts,
            meta={"green": green, "repo_ref": repo_ref},
        )
    )
    typer.echo(cp.id)


@checkpoint_app.command("restore")
def checkpoint_restore(
    checkpoint_id: str = typer.Argument(..., help="Checkpoint id"),
    path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)"),
) -> None:
    repo_root = find_repo_root(path or Path.cwd())
    git = GitTool(repo_root)
    store = CheckpointsStore(repo_root)
    cp = store.get(checkpoint_id)
    if cp.repo_ref != "no-git" and git.is_repo():
        git.checkout_detach(cp.repo_ref)
    else:
        snap_ptr = next((p for p in cp.artifacts if p.endswith(".snapshot.json") or ".snapshot.json@" in p), None)
        if not snap_ptr:
            raise typer.Exit(code=2)
        store.restore_snapshot(snap_ptr)
    ledger = Ledger(repo_root, branch_id="main")
    ledger.append(
        new_event(
            agent="router",
            type="ROLLBACK_APPLIED",
            summary=f"Restored checkpoint {checkpoint_id}",
            branch_id="main",
            pointers=[],
        )
    )
    typer.echo(checkpoint_id)


@branch_app.command("create")
def branch_create(
    checkpoint_id: str = typer.Option(..., "--from", help="Checkpoint id to branch from"),
    name: Optional[str] = typer.Option(None, "--name", help="New git branch name"),
    path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)"),
) -> None:
    repo_root = find_repo_root(path or Path.cwd())
    store = CheckpointsStore(repo_root)
    cp = store.get(checkpoint_id)
    git = GitTool(repo_root)
    if not git.is_repo():
        raise typer.Exit(code=2)
    branch_name = name or f"vibe/{checkpoint_id}"
    git.branch_create(branch_name, cp.repo_ref)
    git.checkout(branch_name)

    # Ledger stream for branch
    branch_ledger = Ledger(repo_root, branch_id=branch_name)
    branch_ledger.ensure()
    branch_dir = repo_root / ".vibe" / "branches" / branch_name
    branch_dir.mkdir(parents=True, exist_ok=True)
    (branch_dir / "branch.json").write_text(
        json.dumps(
            {
                "id": branch_name,
                "git_branch": branch_name,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "derived_from": checkpoint_id,
                "repo_ref": cp.repo_ref,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    main_ledger = Ledger(repo_root, branch_id="main")
    main_ledger.append(
        new_event(
            agent="router",
            type="BRANCH_CREATED",
            summary=f"Created branch {branch_name} from {checkpoint_id}",
            branch_id="main",
            pointers=[],
            meta={"branch": branch_name, "derived_from": checkpoint_id},
        )
    )
    typer.echo(branch_name)
