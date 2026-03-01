from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


_IGNORE_DIRS = {".git", ".vibe", "__pycache__", ".venv", "venv", "node_modules", "dist", "build", ".pytest_cache"}


def _walk_files(repo_root: Path) -> Iterable[Path]:
    for path in repo_root.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(repo_root)
        if rel.parts and rel.parts[0] in _IGNORE_DIRS:
            continue
        yield path


def _basic_tree(repo_root: Path, *, max_entries: int = 200) -> str:
    entries = []
    for child in sorted(repo_root.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if child.name in _IGNORE_DIRS:
            continue
        entries.append(child.name + ("/" if child.is_dir() else ""))
        if len(entries) >= max_entries:
            entries.append("…")
            break
    return "\n".join(f"- {e}" for e in entries) + ("\n" if entries else "")


def generate_project_manifest(repo_root: Path) -> str:
    files = list(_walk_files(repo_root))
    ext_counts: Counter[str] = Counter()
    for p in files:
        ext = p.suffix.lower() or "<none>"
        ext_counts[ext] += 1

    hints = []
    if (repo_root / "pyproject.toml").exists():
        hints.append("- Python: `pyproject.toml` detected")
    if (repo_root / "package.json").exists():
        hints.append("- Node: `package.json` detected")
    if (repo_root / "Makefile").exists():
        hints.append("- Build: `Makefile` detected")

    top_exts = "\n".join(f"- `{ext}`: {cnt}" for ext, cnt in ext_counts.most_common(12))

    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return (
        "# Project Manifest\n\n"
        f"- Generated at: `{ts}`\n"
        f"- Repo root: `{repo_root}`\n\n"
        "## Top-level entries\n\n"
        f"{_basic_tree(repo_root)}\n"
        "## Language / build hints\n\n"
        f"{(''.join(h + '\\n' for h in hints) or '- (none detected)\\n')}\n"
        "## File extensions (top)\n\n"
        f"{(top_exts or '- (no files)')}\n"
    )


def generate_run_manifest(repo_root: Path) -> str:
    lines = ["# Run Manifest", ""]
    if (repo_root / "pyproject.toml").exists():
        lines += [
            "## Python",
            "",
            "```bash",
            "pip install -e .",
            "pytest -q",
            "```",
            "",
        ]
    if (repo_root / "package.json").exists():
        lines += [
            "## Node",
            "",
            "```bash",
            "npm install",
            "npm test",
            "```",
            "",
        ]

    readme = repo_root / "README.md"
    if readme.exists():
        lines += [
            "## README hints",
            "",
            "(This section is not authoritative; it is copied from README snippets.)",
            "",
        ]
        text = readme.read_text(encoding="utf-8", errors="replace")
        snippet = "\n".join(text.splitlines()[:80])
        lines += ["```text", snippet, "```", ""]

    return "\n".join(lines) + "\n"


def generate_vibe_system_manifest(repo_root: Path) -> str:
    """
    A self-describing manifest so agents can understand how Vibe operates
    in the current workspace. This is intentionally short and actionable.
    """
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return (
        "# Vibe System Manifest\n\n"
        f"- Generated at: `{ts}`\n"
        f"- Workspace: `{repo_root}`\n\n"
        "## 目录结构（.vibe）\n\n"
        "- `.vibe/vibe.yaml`：配置（providers/agents/routes/policy）\n"
        "- `.vibe/ledger.jsonl`：事件账本（JSONL 追加写）\n"
        "- `.vibe/artifacts/`：命令输出/patch/报告等工件（内容寻址去重）\n"
        "- `.vibe/checkpoints.json`：检查点（green/restore_steps/meta）\n"
        "- `.vibe/views/<agent_id>/`：各工种的记忆域（memory.jsonl/bookmarks/rollbacks）\n"
        "- `.vibe/manifests/`：派生索引（project/run/repo_overview 等）\n\n"
        "## 三种权限模式（policy.mode）\n\n"
        "- `chat_only`：仅聊天（只读：可扫描/读取/搜索；不运行命令、不写代码、不改 git）\n"
        "- `prompt`：逐项询问授权（每次工具调用都会弹窗确认/拒绝）\n"
        "- `allow_all`：完全授权（不询问，按策略自动执行）\n\n"
        "## 路由等级（L0–L4）\n\n"
        "- `L0`：快速（草稿验证；router/coder_backend/qa(smoke)；不标绿）\n"
        "- `L1`：简单 MVP（默认；pm/router/coder_backend/qa(unit+lint)；通过才 green；env_engineer 按需补齐可运行命令/交付说明）\n"
        "- `L2`：多模块 MVP（跨模块/契约/鉴权等；加 requirements_analyst/architect/api_confirm/code_reviewer；必须 review；QA 升级）\n"
        "- `L3`：可发布（交付/可复现；加 env/security/doc/release 等门禁）\n"
        "- `L4`：生产级（高风险；含 perf/compliance/runbook/迁移回滚等门禁）\n\n"
        "## VS Code / 终端如何触发执行\n\n"
        "- VS Code（写项目模式）：先对话梳理需求；信息足够时会自动触发工作流并落地到代码。系统会自动：\n"
        "  1) `vibe task add ...` 写入任务；2) `vibe run` 运行工作流；3) 产出 checkpoint + ledger 事件。\n"
        "- 终端：\n"
        "  - 初始化：`vibe init`\n"
        "  - 添加任务：`vibe task add \"...\"`\n"
        "  - 执行工作流：`vibe run --route auto`\n\n"
        "## 事实与审计规则（非常重要）\n\n"
        "- 禁止“装跑”：任何声称运行过命令，必须来自 `cmd.run` 的真实 stdout/stderr，并保存到 artifacts。\n"
        "- 事实源必须可追溯：文件指针（path#Lx-Ly@sha256）、artifact 指针、git commit/ref。\n"
        "- 如果工作流修复循环结束仍存在阻塞，会创建非绿灯检查点（reason=`fix_loop_blockers`），供继续修复。\n"
    )


def write_manifests(repo_root: Path) -> None:
    vibe_dir = repo_root / ".vibe" / "manifests"
    vibe_dir.mkdir(parents=True, exist_ok=True)
    (vibe_dir / "project_manifest.md").write_text(generate_project_manifest(repo_root), encoding="utf-8")
    (vibe_dir / "run_manifest.md").write_text(generate_run_manifest(repo_root), encoding="utf-8")
    (vibe_dir / "vibe_system.md").write_text(generate_vibe_system_manifest(repo_root), encoding="utf-8")
