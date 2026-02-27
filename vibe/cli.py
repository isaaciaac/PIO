from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from vibe.config import VibeConfig, default_config, write_default_config
from vibe.repo import ensure_vibe_dirs, find_repo_root

app = typer.Typer(help="vibe coding / multi-agent orchestrator (MVP)")
config_app = typer.Typer(help="Config commands")
task_app = typer.Typer(help="Task commands")

app.add_typer(config_app, name="config")
app.add_typer(task_app, name="task")


@app.command()
def init(path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)")) -> None:
    repo_root = find_repo_root(path or Path.cwd())
    ensure_vibe_dirs(repo_root)
    write_default_config(repo_root, default_config())
    typer.echo(f"Initialized .vibe in {repo_root}")


@config_app.command("show")
def config_show(path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)")) -> None:
    repo_root = find_repo_root(path or Path.cwd())
    cfg_path = repo_root / ".vibe" / "vibe.yaml"
    if not cfg_path.exists():
        raise typer.Exit(code=2)
    cfg = VibeConfig.load(cfg_path)
    typer.echo(json.dumps(cfg.redacted().model_dump(), ensure_ascii=False, indent=2))


@task_app.command("add")
def task_add(
    text: str = typer.Argument(..., help="Task description"),
    path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)"),
) -> None:
    repo_root = find_repo_root(path or Path.cwd())
    ledger_path = repo_root / ".vibe" / "ledger.jsonl"
    if not ledger_path.exists():
        raise typer.Exit(code=2)
    event = {
        "id": f"task_{int(__import__('time').time())}",
        "ts": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "branch_id": "main",
        "agent": "user",
        "type": "REQ_CREATED",
        "summary": text.strip().splitlines()[0][:200],
        "pointers": [],
        "meta": {"text": text},
    }
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    typer.echo(event["id"])

