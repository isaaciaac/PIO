from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from vibe.config import VibeConfig, default_config, write_default_config
from vibe.manifests import write_manifests
from vibe.repo import ensure_vibe_dirs, find_repo_root
from vibe.schemas.events import new_event
from vibe.storage.ledger import Ledger

app = typer.Typer(help="vibe coding / multi-agent orchestrator (MVP)")
config_app = typer.Typer(help="Config commands")
task_app = typer.Typer(help="Task commands")

app.add_typer(config_app, name="config")
app.add_typer(task_app, name="task")


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
    ledger = Ledger(repo_root)
    event = new_event(
        agent="user",
        type="REQ_CREATED",
        summary=text.strip().splitlines()[0][:200],
        meta={"text": text},
    )
    ledger.append(event)
    typer.echo(event.id)
