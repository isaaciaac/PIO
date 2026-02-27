from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional
from uuid import uuid4


PolicyMode = Literal["allow_all", "prompt", "chat_only"]


class PolicyDeniedError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolRequest:
    id: str
    ts: str
    agent_id: str
    tool: str
    detail: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ToolPolicy:
    def __init__(self, *, mode: PolicyMode) -> None:
        self.mode = mode
        self._session_allow_all = False

    def check(self, *, agent_id: str, tool: str, detail: str) -> None:
        if self.mode == "allow_all" or self._session_allow_all:
            return
        if self.mode == "chat_only":
            raise PolicyDeniedError(f"Denied by policy(chat_only): {agent_id} -> {tool}: {detail}")
        if self.mode != "prompt":
            raise PolicyDeniedError(f"Denied by unknown policy mode {self.mode!r}")

        if os.getenv("VIBE_APPROVAL_DIR"):
            allowed = self._prompt_file(agent_id=agent_id, tool=tool, detail=detail)
        else:
            allowed = self._prompt_tty(agent_id=agent_id, tool=tool, detail=detail)

        if allowed is True:
            return
        raise PolicyDeniedError(f"Denied by user: {agent_id} -> {tool}: {detail}")

    def _prompt_tty(self, *, agent_id: str, tool: str, detail: str) -> bool:
        if not sys.stdin.isatty():
            raise PolicyDeniedError(
                "Policy mode is 'prompt' but stdin is not interactive. "
                "Run in a terminal, or set policy mode to 'allow_all' or 'chat_only'."
            )

        prompt = f"[vibe] Allow {agent_id} to use {tool}?\n  {detail}\n[y]es / [n]o / [a]llow all (session): "
        while True:
            ans = input(prompt).strip().lower()
            if ans in {"y", "yes"}:
                return True
            if ans in {"n", "no", ""}:
                return False
            if ans in {"a", "all"}:
                self._session_allow_all = True
                return True

    def _prompt_file(self, *, agent_id: str, tool: str, detail: str) -> bool:
        approval_root = Path(os.environ["VIBE_APPROVAL_DIR"]).resolve()
        req_dir = approval_root / "requests"
        resp_dir = approval_root / "responses"
        req_dir.mkdir(parents=True, exist_ok=True)
        resp_dir.mkdir(parents=True, exist_ok=True)

        req = ToolRequest(
            id=f"req_{uuid4().hex[:12]}",
            ts=_now_iso(),
            agent_id=agent_id,
            tool=tool,
            detail=detail,
        )
        req_path = req_dir / f"{req.id}.json"
        resp_path = resp_dir / f"{req.id}.json"

        req_path.write_text(json.dumps(req.__dict__, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        timeout_s = int(os.getenv("VIBE_APPROVAL_TIMEOUT_S", "600"))
        started = time.time()
        while time.time() - started < timeout_s:
            if resp_path.exists():
                raw = resp_path.read_text(encoding="utf-8")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    raise PolicyDeniedError(f"Invalid approval response JSON: {resp_path}")
                allow = bool(data.get("allow", False))
                # Best-effort cleanup
                try:
                    req_path.unlink(missing_ok=True)
                except OSError:
                    pass
                try:
                    resp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                return allow
            time.sleep(0.2)

        raise PolicyDeniedError(
            f"Timed out waiting for approval response (timeout={timeout_s}s). "
            f"Request: {req_path}"
        )


def resolve_policy_mode(config_mode: PolicyMode, *, override: Optional[str] = None) -> PolicyMode:
    mode = config_mode
    env = os.getenv("VIBE_POLICY_MODE")
    if env:
        mode = env.strip()
    if override:
        mode = override.strip()
    if mode not in {"allow_all", "prompt", "chat_only"}:
        raise ValueError(f"Invalid policy mode: {mode!r}")
    return mode  # type: ignore[return-value]

