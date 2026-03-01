from __future__ import annotations

import pytest

from vibe.policy import PolicyDeniedError, ToolPolicy


def test_policy_chat_only_allows_readonly_tools_and_denies_writes() -> None:
    p = ToolPolicy(mode="chat_only")

    # Read-only tools
    p.check(agent_id="pm", tool="read_file", detail="read README.md (lines 1-200)")
    p.check(agent_id="router", tool="scan_repo", detail="scan repo to refresh .vibe/manifests (reason=chat)")
    p.check(agent_id="pm", tool="search", detail="rg 'TODO' (cwd=.)")

    # Read-only git operations
    p.check(agent_id="router", tool="git", detail="git rev-parse HEAD")
    p.check(agent_id="router", tool="git", detail="git rev-parse --abbrev-ref HEAD")
    p.check(agent_id="router", tool="git", detail="git rev-parse --is-inside-work-tree")
    p.check(agent_id="router", tool="git", detail="git diff --numstat")
    p.check(agent_id="router", tool="git", detail="git diff")

    # Write/execute should be denied
    with pytest.raises(PolicyDeniedError):
        p.check(agent_id="coder_backend", tool="write_file", detail="write src/app.py (bytes=12)")

    with pytest.raises(PolicyDeniedError):
        p.check(agent_id="qa", tool="run_cmd", detail="run ['pytest', '-q'] (cwd=.)")

    with pytest.raises(PolicyDeniedError):
        p.check(agent_id="router", tool="git", detail="git checkout main")

    with pytest.raises(PolicyDeniedError):
        p.check(agent_id="router", tool="git", detail="git commit -m 'msg'")

