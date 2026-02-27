from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Union

from vibe.storage.artifacts import ArtifactsStore, ArtifactPointer


Cmd = Union[str, Sequence[str]]


@dataclass(frozen=True)
class CmdResult:
    cmd: List[str] | str
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    meta: str


class CmdTool:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.artifacts = ArtifactsStore(repo_root)

    def run(self, cmd: Cmd, *, cwd: Optional[Path] = None, timeout_s: int = 600) -> CmdResult:
        run_cwd = str((cwd or self.repo_root).resolve())
        if isinstance(cmd, str):
            proc = subprocess.run(
                cmd,
                cwd=run_cwd,
                shell=True,
                capture_output=True,
                timeout=timeout_s,
            )
            cmd_repr: List[str] | str = cmd
        else:
            proc = subprocess.run(
                list(cmd),
                cwd=run_cwd,
                shell=False,
                capture_output=True,
                timeout=timeout_s,
            )
            cmd_repr = list(cmd)

        stdout_ptr = self.artifacts.put_bytes(proc.stdout, suffix=".stdout.txt", kind="cmd_stdout").to_pointer()
        stderr_ptr = self.artifacts.put_bytes(proc.stderr, suffix=".stderr.txt", kind="cmd_stderr").to_pointer()
        meta_ptr = self.artifacts.put_json(
            {
                "cmd": cmd_repr,
                "cwd": run_cwd,
                "returncode": proc.returncode,
                "env": {"VIBE_MOCK_MODE": os.getenv("VIBE_MOCK_MODE", "")},
            },
            suffix=".cmd.json",
            kind="cmd_meta",
        ).to_pointer()

        return CmdResult(
            cmd=cmd_repr,
            cwd=run_cwd,
            returncode=proc.returncode,
            stdout=stdout_ptr,
            stderr=stderr_ptr,
            meta=meta_ptr,
        )

