from __future__ import annotations

import os
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

import typer

from vibe.config import VibeConfig, default_config, write_default_config
from vibe.agents.registry import AGENT_REGISTRY
from vibe.manifests import write_manifests
from vibe.orchestrator import Orchestrator
from vibe.context import effective_context_config, maybe_compress_chat_history, read_memory_records
from vibe.policy import PolicyDeniedError, ToolPolicy, resolve_policy_mode
from vibe.repo import ensure_vibe_dirs, find_repo_root
from vibe.schemas import packs
from vibe.schemas.memory import ChatDigest
from vibe.storage.checkpoints import CheckpointsStore
from vibe.schemas.events import new_event
from vibe.storage.ledger import Ledger
from vibe.storage.ledger import ledger_path
from vibe.toolbox import Toolbox
from vibe.style import normalize_style, style_prompt, style_temperature
from vibe.secrets import apply_workspace_secrets

app = typer.Typer(help="vibe coding / multi-agent orchestrator (MVP)")
config_app = typer.Typer(help="Config commands")
task_app = typer.Typer(help="Task commands")
checkpoint_app = typer.Typer(help="Checkpoint commands")
branch_app = typer.Typer(help="Branch commands")

app.add_typer(config_app, name="config")
app.add_typer(task_app, name="task")
app.add_typer(checkpoint_app, name="checkpoint")
app.add_typer(branch_app, name="branch")


@app.callback()
def _global_options(
    ctx: typer.Context,
    policy: Optional[str] = typer.Option(None, "--policy", help="Tool permission mode: allow_all|prompt|chat_only"),
) -> None:
    ctx.ensure_object(dict)
    ctx.obj["policy"] = policy


def _make_toolbox(repo_root: Path, *, policy_override: Optional[str]) -> Toolbox:
    cfg_path = repo_root / ".vibe" / "vibe.yaml"
    cfg = VibeConfig.load(cfg_path)
    policy = ToolPolicy(mode=resolve_policy_mode(cfg.policy.mode, override=policy_override))
    return Toolbox(repo_root, config=cfg, policy=policy)


def _detect_branch_id(repo_root: Path, tools: Toolbox) -> str:
    try:
        branch = tools.git_current_branch(agent_id="router")
    except Exception:
        return "main"
    if branch in {"main", "master"}:
        return "main"
    if branch == "HEAD":
        return "main"
    if ledger_path(repo_root, branch).exists():
        return branch
    return "main"


@app.command()
def init(path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)")) -> None:
    repo_root = find_repo_root(path or Path.cwd())
    cfg = default_config()
    ensure_vibe_dirs(repo_root, agent_ids=list(cfg.agents.keys()))
    write_default_config(repo_root, cfg)
    write_manifests(repo_root)
    typer.echo(f"Initialized .vibe in {repo_root}")


@app.command()
def scan(
    ctx: typer.Context,
    path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)"),
    force: bool = typer.Option(False, "--force", help="Force re-scan even if up-to-date"),
) -> None:
    repo_root = find_repo_root(path or Path.cwd())
    cfg_path = repo_root / ".vibe" / "vibe.yaml"
    if not cfg_path.exists():
        raise typer.Exit(code=2)
    tools = _make_toolbox(repo_root, policy_override=(ctx.obj or {}).get("policy"))
    try:
        status = tools.scan_repo(agent_id="router", reason="manual", force=force)
        typer.echo(status)
    except PolicyDeniedError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=3)


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
    ctx: typer.Context,
    text: str = typer.Argument(..., help="Task description"),
    path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)"),
) -> None:
    repo_root = find_repo_root(path or Path.cwd())
    if not (repo_root / ".vibe" / "ledger.jsonl").exists():
        raise typer.Exit(code=2)
    tools = _make_toolbox(repo_root, policy_override=(ctx.obj or {}).get("policy"))
    branch_id = _detect_branch_id(repo_root, tools)
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


def _chat_history_path(repo_root: Path, agent_id: str) -> Path:
    return repo_root / ".vibe" / "views" / agent_id / "chat.jsonl"


def _read_chat_history(path: Path, *, limit: int) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    items: list[dict[str, str]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = str(data.get("role") or "").strip()
        content = str(data.get("content") or "").strip()
        if role in {"user", "assistant", "system"} and content:
            items.append({"role": role, "content": content})
    return items


def _append_chat_history(path: Path, *, role: str, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "role": role,
        "content": content,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _force_scan_for_chat(message: str) -> bool:
    """
    Heuristic: force a repo re-scan when the user is explicitly asking for
    repo-level facts (structure/how to run/deps) or asks to refresh/re-scan.

    Normal messages still run a stale-check scan, which is cheaper.
    """
    text = (message or "").strip()
    if not text:
        return False
    low = text.lower()

    explicit = [
        "/scan",
        "vibe scan",
        "scan",
        "rescan",
        "refresh",
        "重新扫描",
        "再扫描",
        "重新索引",
        "索引一下",
        "更新索引",
        "更新扫描",
        "再查一遍",
        "重新查",
        "重读",
        "再读一遍",
        "reload",
    ]
    if any(k in low or k in text for k in explicit):
        return True

    repo_facts = [
        "这个项目",
        "当前项目",
        "仓库",
        "目录",
        "目录结构",
        "文件结构",
        "文件列表",
        "有哪些文件",
        "有哪些目录",
        "项目结构",
        "怎么运行",
        "如何运行",
        "怎么启动",
        "如何启动",
        "怎么测试",
        "如何测试",
        "依赖",
        "脚本",
        "启动命令",
        "测试命令",
        "构建",
        "build",
        "install",
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "makefile",
        "dockerfile",
        "compose",
        "devcontainer",
        "进度",
        "当前状态",
    ]
    if any(k in text or k in low for k in repo_facts):
        # Narrow "项目" questions to avoid over-triggering on generic tasks.
        if "项目" in text and not any(w in text for w in ["是什么", "做什么", "结构", "目录", "文件", "怎么", "如何", "运行", "启动", "测试", "依赖"]):
            return False
        return True

    return False


@app.command()
def chat(
    ctx: typer.Context,
    message: str = typer.Argument(..., help="Chat message"),
    agent: str = typer.Option("pm", "--agent", help="Agent id to chat with (default: pm)"),
    path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)"),
    mock: bool = typer.Option(False, "--mock", help="Force mock mode for this chat"),
    style: Optional[str] = typer.Option(None, "--style", help="Chat style: free|balanced|detailed (overrides vibe.yaml)"),
    json_out: bool = typer.Option(False, "--json", help="Output ChatReply JSON"),
    reset: bool = typer.Option(False, "--reset", help="Reset saved chat history for this workspace"),
    history: int = typer.Option(16, "--history", help="How many previous messages to include"),
) -> None:
    if mock:
        os.environ["VIBE_MOCK_MODE"] = "1"
    repo_root = find_repo_root(path or Path.cwd())
    cfg_path = repo_root / ".vibe" / "vibe.yaml"
    if not cfg_path.exists():
        raise typer.Exit(code=2)

    cfg = VibeConfig.load(cfg_path)
    apply_workspace_secrets(repo_root, providers=cfg.providers)
    agent_id = agent.strip()
    agent_cfg = cfg.agents.get(agent_id)
    if not agent_cfg:
        typer.echo(f"Missing agent config: {agent_id}", err=True)
        raise typer.Exit(code=2)
    agent_cls = AGENT_REGISTRY.get(agent_id)
    if not agent_cls:
        typer.echo(f"Missing agent implementation: {agent_id}", err=True)
        raise typer.Exit(code=2)

    hist_path = _chat_history_path(repo_root, agent_id)
    mem_path = repo_root / ".vibe" / "views" / agent_id / "memory.jsonl"
    if reset:
        hist_path.write_text("", encoding="utf-8")

    a = agent_cls(agent_cfg, providers=cfg.providers)

    def digest_builder(text: str) -> ChatDigest:
        # Chunk to avoid over-long single requests.
        chunk_chars = 6000
        chunks = [text[i : i + chunk_chars] for i in range(0, len(text), chunk_chars)]
        chunks = chunks[:6]
        partials: list[ChatDigest] = []
        system_digest = (
            "你是 Vibe 的上下文压缩器。你必须只输出 JSON（不要 markdown），并严格匹配 ChatDigest schema："
            "{summary: string, pinned: string[], background: string[], open_questions: string[]}。\n"
            "规则：\n"
            "- pinned：挑出用户的高权重要求/约束/验收（不可丢失），每条不超过 120 字\n"
            "- background：低权重背景信息/闲聊，每条不超过 120 字\n"
            "- open_questions：为了继续执行需要追问的问题（如果没有就留空）\n"
            "- 不要输出任何额外 key；不要把内容包在最外层对象里。"
        )
        for c in chunks:
            d, _ = a.chat_json(schema=ChatDigest, system=system_digest, user=f"对话片段：\n{c}\n")
            partials.append(d)
        if not partials:
            return ChatDigest(summary="（无内容）", pinned=[], background=[], open_questions=[])

        def uniq(items: list[str]) -> list[str]:
            out: list[str] = []
            seen: set[str] = set()
            for it in items:
                s = str(it).strip()
                if not s or s in seen:
                    continue
                seen.add(s)
                out.append(s)
            return out

        summary = "；".join([p.summary.strip() for p in partials if p.summary.strip()])[:240] or partials[0].summary[:240]
        pinned = uniq([x for p in partials for x in (p.pinned or [])])[:8]
        background = uniq([x for p in partials for x in (p.background or [])])[:12]
        open_questions = uniq([x for p in partials for x in (p.open_questions or [])])[:8]
        return ChatDigest(summary=summary, pinned=pinned, background=background, open_questions=open_questions)

    maybe_compress_chat_history(
        repo_root=repo_root,
        agent_id=agent_id,
        cfg=cfg,
        hist_path=hist_path,
        memory_path=mem_path,
        incoming_user_message=message,
        history_limit=max(0, min(history, 64)),
        digest_builder=digest_builder,
    )

    past = _read_chat_history(hist_path, limit=max(0, min(history, 64)))
    purpose = (agent_cfg.purpose or "").strip()
    role_hint = f"你的工种ID是 {agent_id}。" + (f" 你的职责是：{purpose}。" if purpose else "")
    resolved_style = normalize_style(style or os.getenv("VIBE_STYLE") or getattr(cfg.behavior, "style", "balanced"))
    style_text = style_prompt(resolved_style)

    # Ground chat with a small set of repo facts when policy allows (read-only).
    repo_chunks: list[str] = []
    repo_pointers: list[str] = []
    try:
        tools = _make_toolbox(repo_root, policy_override=(ctx.obj or {}).get("policy"))
    except Exception:
        tools = None
    if tools is not None:
        # Auto scan on first question / when stale, so the agent can ground in repo facts.
        try:
            tools.scan_repo(agent_id="router", reason="chat", force=_force_scan_for_chat(message))
        except PolicyDeniedError:
            pass
        except Exception:
            pass

        for rel in [
            ".vibe/manifests/vibe_system.md",
            ".vibe/manifests/repo_overview.md",
            ".vibe/manifests/run_manifest.md",
            ".vibe/manifests/project_manifest.md",
            "README.md",
            "package.json",
            "pyproject.toml",
        ]:
            try:
                rr = tools.read_file(agent_id=agent_id, path=rel, start_line=1, end_line=200)
                repo_pointers.append(rr.pointer)
                repo_chunks.append(f"<<< {rr.pointer} >>>\n{rr.content}\n")
            except PolicyDeniedError:
                continue
            except Exception:
                continue

    ledger_lines: list[str] = []
    try:
        # Main ledger is always present; this gives the agent a sense of "what happened recently".
        led = Ledger(repo_root, branch_id="main")
        for e in led.iter_events(limit=20, reverse=True):
            ledger_lines.append(f"- {e.ts} {e.agent} {e.type}: {e.summary}")
    except Exception:
        ledger_lines = []

    repo_context_text = "\n".join(repo_chunks).strip()
    ledger_context_text = "\n".join(ledger_lines).strip()
    client = (os.getenv("VIBE_CLIENT") or "").strip().lower()
    vscode_env = bool(os.getenv("VSCODE_PID") or (os.getenv("TERM_PROGRAM") or "").strip().lower() == "vscode")
    if client == "vscode" or vscode_env:
        exec_hint = (
            "如果用户希望你继续动手写项目：不要让用户去手敲命令，也不要要求用户回复特定触发词（如「执行」或「/run」）。"
            "你只需追问缺失信息；当信息足够时，直接说明你将开始落地实现，并给出接下来会发生什么（由系统自动触发工作流）。"
        )
    else:
        exec_hint = (
            "如果用户希望你继续动手写项目：提示用户在终端运行 `vibe task add \"...\"` 然后 `vibe run`；"
            "或使用 VS Code 扩展在写项目模式下继续描述需求（信息足够会自动执行）。"
        )

    system = (
        f"你是 Vibe 系统里的一个工种代理。{role_hint}\n\n"
        "你要用自然语言与用户对话，帮助用户把问题变成可执行的下一步（必要时给出验收标准/风险点/排障步骤）。\n\n"
        "重要：你现在只是在“对话”模式下回答问题，不能直接运行命令、修改文件、创建分支或提交代码。"
        "不要声称“已经生成/已经创建/已经运行/已经修改”。\n"
        "当用户只是询问现状/进度/怎么运行/为什么失败时，你应直接基于可追溯事实片段回答，不要反复要求用户先运行什么。\n"
        f"{exec_hint}\n\n"
        f"{style_text}\n\n"
        "硬约束：你必须只输出 JSON（不要 markdown），并严格匹配 ChatReply schema："
        "{reply: string, suggested_actions: string[], pointers: string[]}。\n"
        "不要在最外层包一层额外的 key。"
    )

    if repo_context_text or ledger_context_text:
        system = (
            f"{system}\n\n"
            "以下是来自本地仓库的可追溯事实片段（你可以据此回答；不要编造；引用这些内容时，尽量把相关指针写入输出的 pointers 字段）：\n\n"
            f"{(repo_context_text + '\\n\\n') if repo_context_text else ''}"
            f"{('<<< 最近账本事件（新→旧） >>>\\n' + ledger_context_text) if ledger_context_text else ''}"
        )
    ctx_cfg = effective_context_config(cfg, agent_id=agent_id)
    mem = read_memory_records(mem_path, limit=max(0, min(ctx_cfg.keep_last_digests, 10)))
    mem_text = ""
    if mem:
        lines: list[str] = []
        lines.append("以下是该工种的结构化记忆摘要（已自动压缩历史对话；事实以 pointers 展开为准）：")
        for r in mem[-max(1, ctx_cfg.keep_last_digests) :]:
            pinned = [s for s in (r.digest.pinned or [])][:3]
            pin = ("；".join(pinned))[:240] if pinned else ""
            ptrs = ", ".join(list(r.pointers or [])[:2])
            lines.append(f"- {r.digest.summary.strip()[:200]}")
            if pin:
                lines.append(f"  要点: {pin}")
            if ptrs:
                lines.append(f"  pointers: {ptrs}")
        mem_text = "\n".join(lines).strip()

    messages = [{"role": "system", "content": system}]
    if mem_text:
        messages.append({"role": "system", "content": mem_text})
    messages.extend(past)
    messages.append({"role": "user", "content": message})
    reply, _meta = a.chat_json(schema=packs.ChatReply, messages=messages, user=message, temperature=style_temperature(resolved_style))

    _append_chat_history(hist_path, role="user", content=message)
    _append_chat_history(hist_path, role="assistant", content=reply.reply)

    if json_out:
        typer.echo(reply.model_dump_json(indent=2, ensure_ascii=False))
    else:
        typer.echo(reply.reply)


@app.command()
def run(
    ctx: typer.Context,
    task: Optional[str] = typer.Option(None, "--task", help="Task event id (default: latest)"),
    path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)"),
    mock: bool = typer.Option(False, "--mock", help="Force mock mode for this run"),
    mock_writes: bool = typer.Option(False, "--mock-writes", help="In mock mode, enable deterministic file writes"),
    route: str = typer.Option("auto", "--route", help="Route level: auto|L0|L1|L2|L3|L4"),
    style: Optional[str] = typer.Option(None, "--style", help="Workflow style: free|balanced|detailed (overrides vibe.yaml)"),
) -> None:
    if mock:
        os.environ["VIBE_MOCK_MODE"] = "1"
    if mock_writes:
        os.environ["VIBE_MOCK_WRITES"] = "1"
    repo_root = find_repo_root(path or Path.cwd())
    try:
        cfg_path = repo_root / ".vibe" / "vibe.yaml"
        if cfg_path.exists():
            cfg = VibeConfig.load(cfg_path)
            apply_workspace_secrets(repo_root, providers=cfg.providers)
        orch = Orchestrator(repo_root, policy_mode=(ctx.obj or {}).get("policy"))
        result = orch.run(task_id=task, route=route, style=style)
        typer.echo(result.checkpoint_id)
    except PolicyDeniedError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=3)


@checkpoint_app.command("list")
def checkpoint_list(path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)")) -> None:
    repo_root = find_repo_root(path or Path.cwd())
    store = CheckpointsStore(repo_root)
    typer.echo(store.load().model_dump_json(indent=2))


@checkpoint_app.command("create")
def checkpoint_create(
    ctx: typer.Context,
    label: str = typer.Option("manual", "--label", help="Checkpoint label"),
    green: bool = typer.Option(False, "--green", help="Mark checkpoint as green"),
    path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)"),
) -> None:
    repo_root = find_repo_root(path or Path.cwd())
    tools = _make_toolbox(repo_root, policy_override=(ctx.obj or {}).get("policy"))
    branch_id = _detect_branch_id(repo_root, tools)
    ledger = Ledger(repo_root, branch_id=branch_id)
    store = CheckpointsStore(repo_root)
    repo_ref = "no-git"
    try:
        repo_ref = tools.git_head_sha(agent_id="router")
    except PolicyDeniedError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=3)
    except Exception:
        repo_ref = "no-git"
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
    ctx: typer.Context,
    checkpoint_id: str = typer.Argument(..., help="Checkpoint id"),
    path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)"),
) -> None:
    repo_root = find_repo_root(path or Path.cwd())
    tools = _make_toolbox(repo_root, policy_override=(ctx.obj or {}).get("policy"))
    store = CheckpointsStore(repo_root)
    cp = store.get(checkpoint_id)
    if cp.repo_ref != "no-git":
        try:
            tools.git_checkout_detach(agent_id="router", ref=cp.repo_ref)
        except PolicyDeniedError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(code=3)
        except Exception:
            pass
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
    ctx: typer.Context,
    checkpoint_id: str = typer.Option(..., "--from", help="Checkpoint id to branch from"),
    name: Optional[str] = typer.Option(None, "--name", help="New git branch name"),
    path: Optional[Path] = typer.Option(None, "--path", help="Repo path (default: cwd)"),
) -> None:
    repo_root = find_repo_root(path or Path.cwd())
    store = CheckpointsStore(repo_root)
    cp = store.get(checkpoint_id)
    tools = _make_toolbox(repo_root, policy_override=(ctx.obj or {}).get("policy"))
    try:
        if not tools.git_is_repo(agent_id="router"):
            raise typer.Exit(code=2)
    except PolicyDeniedError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=3)
    branch_name = name or f"vibe/{checkpoint_id}"
    try:
        tools.git_branch_create(agent_id="router", name=branch_name, ref=cp.repo_ref)
        tools.git_checkout(agent_id="router", ref=branch_name)
    except PolicyDeniedError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=3)

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
