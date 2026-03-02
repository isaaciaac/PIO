from __future__ import annotations

import fnmatch
from typing import Optional, Sequence

from vibe.config import OwnershipRule


class OwnershipDeniedError(RuntimeError):
    def __init__(self, *, agent_id: str, path: str, rule: OwnershipRule) -> None:
        owners = ", ".join(list(rule.owners or [])[:8]) if (rule.owners or []) else "<unset>"
        super().__init__(f"Ownership denied: {agent_id} cannot write {path} (rule={rule.id}, owners=[{owners}])")
        self.agent_id = agent_id
        self.path = path
        self.rule = rule


def normalize_relpath(path: str) -> str:
    return (path or "").replace("\\", "/").lstrip("/")


def match_ownership_rule(*, path: str, rules: Sequence[OwnershipRule]) -> Optional[OwnershipRule]:
    rel = normalize_relpath(path)
    name = rel.split("/")[-1] if rel else ""
    for rule in list(rules or []):
        for pat in list(rule.patterns or []):
            p = str(pat or "").strip()
            if not p:
                continue
            if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p):
                return rule
    return None

