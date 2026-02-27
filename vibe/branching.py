from __future__ import annotations

from pathlib import Path
from typing import Optional

from vibe.storage.ledger import ledger_path
from vibe.tools.git import GitTool


def detect_branch_id(repo_root: Path, *, git: Optional[GitTool] = None) -> str:
    if git and git.is_repo():
        branch = git.current_branch()
        if branch not in {"HEAD", "main", "master"}:
            if ledger_path(repo_root, branch).exists():
                return branch
        if branch in {"main", "master"}:
            return "main"
    return "main"

