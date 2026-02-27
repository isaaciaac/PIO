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


def write_manifests(repo_root: Path) -> None:
    vibe_dir = repo_root / ".vibe" / "manifests"
    vibe_dir.mkdir(parents=True, exist_ok=True)
    (vibe_dir / "project_manifest.md").write_text(generate_project_manifest(repo_root), encoding="utf-8")
    (vibe_dir / "run_manifest.md").write_text(generate_run_manifest(repo_root), encoding="utf-8")

