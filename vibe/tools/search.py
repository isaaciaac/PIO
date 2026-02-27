from __future__ import annotations

from pathlib import Path
from typing import Optional

from vibe.tools.cmd import CmdResult, CmdTool


class SearchTool:
    def __init__(self, repo_root: Path, *, cmd: Optional[CmdTool] = None) -> None:
        self.repo_root = repo_root
        self.cmd = cmd or CmdTool(repo_root)

    def ripgrep(self, query: str, *, cwd: Optional[Path] = None, timeout_s: int = 120) -> CmdResult:
        return self.cmd.run(["rg", query], cwd=cwd or self.repo_root, timeout_s=timeout_s)

