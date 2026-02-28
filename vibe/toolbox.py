from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from vibe.config import VibeConfig
from vibe.policy import PolicyDeniedError, ToolPolicy
from vibe.routes import DiffStats
from vibe.tools.cmd import Cmd, CmdResult, CmdTool
from vibe.tools.fs import FileReadResult, FsTool
from vibe.tools.git import GitCommitResult, GitTool
from vibe.tools.search import SearchTool
from vibe.text import decode_bytes
from vibe.scan import scan_is_stale, write_scan_outputs


def _is_internal_path(path: str) -> bool:
    norm = path.replace("\\", "/").lstrip("/")
    return norm.startswith(".vibe/") or norm.startswith(".git/")


class Toolbox:
    def __init__(self, repo_root: Path, *, config: VibeConfig, policy: ToolPolicy) -> None:
        self.repo_root = repo_root
        self.config = config
        self.policy = policy

        self.cmd = CmdTool(repo_root)
        self.fs = FsTool(repo_root)
        self.git = GitTool(repo_root, cmd=self.cmd)
        self.search = SearchTool(repo_root, cmd=self.cmd)

    def _require_tool_allowed(self, *, agent_id: str, tool: str) -> None:
        agent = self.config.agents.get(agent_id)
        if not agent:
            raise PolicyDeniedError(f"Unknown agent id: {agent_id}")
        if tool not in agent.tools_allowed:
            raise PolicyDeniedError(f"Tool not allowed for agent {agent_id}: {tool}")

    def read_file(
        self,
        *,
        agent_id: str,
        path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
    ) -> FileReadResult:
        if not _is_internal_path(path):
            self._require_tool_allowed(agent_id=agent_id, tool="read_file")
            self.policy.check(agent_id=agent_id, tool="read_file", detail=f"read {path} (lines {start_line or 1}-{end_line or 'EOF'})")
        return self.fs.read_file(path, start_line=start_line, end_line=end_line)

    def write_file(self, *, agent_id: str, path: str, content: str) -> str:
        if not _is_internal_path(path):
            self._require_tool_allowed(agent_id=agent_id, tool="write_file")
            self.policy.check(agent_id=agent_id, tool="write_file", detail=f"write {path} (bytes={len(content.encode('utf-8'))})")
        return self.fs.write_file(path, content)

    def run_cmd(self, *, agent_id: str, cmd: Cmd, cwd: Optional[Path] = None, timeout_s: int = 600) -> CmdResult:
        self._require_tool_allowed(agent_id=agent_id, tool="run_cmd")
        self.policy.check(agent_id=agent_id, tool="run_cmd", detail=f"run {cmd!r} (cwd={cwd or self.repo_root})")
        return self.cmd.run(cmd, cwd=cwd, timeout_s=timeout_s)

    def ripgrep(self, *, agent_id: str, query: str, cwd: Optional[Path] = None, timeout_s: int = 120) -> CmdResult:
        self._require_tool_allowed(agent_id=agent_id, tool="search")
        self.policy.check(agent_id=agent_id, tool="search", detail=f"rg {query!r} (cwd={cwd or self.repo_root})")
        return self.search.ripgrep(query, cwd=cwd, timeout_s=timeout_s)

    def scan_repo(self, *, agent_id: str, reason: str = "", force: bool = False) -> str:
        """
        Scan the repository to refresh .vibe/manifests/* derived facts (read-only for user files).
        Returns a short status string.
        """
        self._require_tool_allowed(agent_id=agent_id, tool="scan_repo")

        max_age_s = int(os.getenv("VIBE_SCAN_MAX_AGE_S", "1800"))
        if not force and not scan_is_stale(self.repo_root, max_age_s=max_age_s):
            return "scan: up-to-date"

        self.policy.check(
            agent_id=agent_id,
            tool="scan_repo",
            detail=f"scan repo to refresh .vibe/manifests (reason={reason or 'auto'})",
        )
        # Refresh derived manifests (tree/run hints) + scan outputs.
        try:
            from vibe.manifests import write_manifests

            write_manifests(self.repo_root)
        except Exception:
            pass
        repo_index, repo_overview, scan_state = write_scan_outputs(self.repo_root)
        return f"scan: wrote {repo_index.name}, {repo_overview.name}, {scan_state.name}"

    # Git wrappers
    def git_head_sha(self, *, agent_id: str) -> str:
        self._require_tool_allowed(agent_id=agent_id, tool="git")
        self.policy.check(agent_id=agent_id, tool="git", detail="git rev-parse HEAD")
        return self.git.head_sha()

    def git_current_branch(self, *, agent_id: str) -> str:
        self._require_tool_allowed(agent_id=agent_id, tool="git")
        self.policy.check(agent_id=agent_id, tool="git", detail="git rev-parse --abbrev-ref HEAD")
        return self.git.current_branch()

    def git_is_repo(self, *, agent_id: str) -> bool:
        self._require_tool_allowed(agent_id=agent_id, tool="git")
        self.policy.check(agent_id=agent_id, tool="git", detail="git rev-parse --is-inside-work-tree")
        return self.git.is_repo()

    def git_checkout_detach(self, *, agent_id: str, ref: str) -> CmdResult:
        self._require_tool_allowed(agent_id=agent_id, tool="git")
        self.policy.check(agent_id=agent_id, tool="git", detail=f"git checkout --detach {ref}")
        return self.git.checkout_detach(ref)

    def git_checkout(self, *, agent_id: str, ref: str) -> CmdResult:
        self._require_tool_allowed(agent_id=agent_id, tool="git")
        self.policy.check(agent_id=agent_id, tool="git", detail=f"git checkout {ref}")
        return self.git.checkout(ref)

    def git_branch_create(self, *, agent_id: str, name: str, ref: str) -> CmdResult:
        self._require_tool_allowed(agent_id=agent_id, tool="git")
        self.policy.check(agent_id=agent_id, tool="git", detail=f"git branch {name} {ref}")
        return self.git.branch_create(name, ref)

    def git_commit(self, *, agent_id: str, message: str, allow_empty: bool = False) -> GitCommitResult:
        self._require_tool_allowed(agent_id=agent_id, tool="git")
        self.policy.check(agent_id=agent_id, tool="git", detail=f"git commit -m {message!r}")
        return self.git.commit(message, allow_empty=allow_empty)

    def git_diff_stats(self, *, agent_id: str) -> DiffStats:
        self._require_tool_allowed(agent_id=agent_id, tool="git")
        self.policy.check(agent_id=agent_id, tool="git", detail="git diff --numstat")
        r = self.git.diff_numstat()
        raw = decode_bytes(self.cmd.artifacts.read_bytes(r.stdout))
        files: list[str] = []
        added = 0
        deleted = 0
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            a, d, path = parts[0].strip(), parts[1].strip(), parts[2].strip()
            files.append(path)
            if a.isdigit():
                added += int(a)
            if d.isdigit():
                deleted += int(d)
        return DiffStats(file_count=len(files), loc_added=added, loc_deleted=deleted, paths=tuple(files), pointer=r.stdout)

    def git_diff(self, *, agent_id: str) -> CmdResult:
        self._require_tool_allowed(agent_id=agent_id, tool="git")
        self.policy.check(agent_id=agent_id, tool="git", detail="git diff")
        return self.git.diff()
