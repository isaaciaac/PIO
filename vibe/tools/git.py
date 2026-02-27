from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from vibe.tools.cmd import CmdResult, CmdTool


class GitError(RuntimeError):
    def __init__(self, message: str, *, result: Optional[CmdResult] = None) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class GitCommitResult:
    commit: str
    cmd: CmdResult


class GitTool:
    def __init__(self, repo_root: Path, *, cmd: Optional[CmdTool] = None) -> None:
        self.repo_root = repo_root
        self.cmd = cmd or CmdTool(repo_root)

    def _run(self, args: List[str], *, timeout_s: int = 600) -> CmdResult:
        result = self.cmd.run(["git", *args], cwd=self.repo_root, timeout_s=timeout_s)
        if result.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed", result=result)
        return result

    def is_repo(self) -> bool:
        try:
            r = self.cmd.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=self.repo_root, timeout_s=30)
            return r.returncode == 0 and self.cmd.artifacts.read_bytes(r.stdout).decode("utf-8", errors="replace").strip() == "true"
        except Exception:
            return False

    def head_sha(self) -> str:
        r = self._run(["rev-parse", "HEAD"], timeout_s=30)
        return self.cmd.artifacts.read_bytes(r.stdout).decode("utf-8", errors="replace").strip()

    def current_branch(self) -> str:
        r = self._run(["rev-parse", "--abbrev-ref", "HEAD"], timeout_s=30)
        return self.cmd.artifacts.read_bytes(r.stdout).decode("utf-8", errors="replace").strip()

    def status(self) -> CmdResult:
        return self._run(["status", "--porcelain=v1"], timeout_s=60)

    def diff(self) -> CmdResult:
        return self._run(["diff"], timeout_s=60)

    def diff_numstat(self) -> CmdResult:
        return self._run(["diff", "--numstat"], timeout_s=60)

    def commit(self, message: str, *, allow_empty: bool = False) -> GitCommitResult:
        args = ["commit", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        r = self._run(args, timeout_s=300)
        return GitCommitResult(commit=self.head_sha(), cmd=r)

    def revert(self, commit: str) -> CmdResult:
        return self._run(["revert", "--no-edit", commit], timeout_s=600)

    def checkout(self, ref: str) -> CmdResult:
        return self._run(["checkout", ref], timeout_s=120)

    def checkout_detach(self, ref: str) -> CmdResult:
        return self._run(["checkout", "--detach", ref], timeout_s=120)

    def branch_create(self, name: str, ref: str) -> CmdResult:
        return self._run(["branch", name, ref], timeout_s=60)

    def worktree_add(self, path: Path, branch: str) -> CmdResult:
        return self._run(["worktree", "add", str(path), branch], timeout_s=600)
