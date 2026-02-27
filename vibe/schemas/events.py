from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class LedgerEvent(BaseModel):
    id: str
    ts: str
    branch_id: str = "main"
    agent: str
    type: str
    summary: str
    pointers: List[str] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)


def new_event(
    *,
    agent: str,
    type: str,
    summary: str,
    branch_id: str = "main",
    pointers: Optional[List[str]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> LedgerEvent:
    return LedgerEvent(
        id=f"evt_{uuid4().hex[:12]}",
        ts=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        branch_id=branch_id,
        agent=agent,
        type=type,
        summary=summary,
        pointers=pointers or [],
        meta=meta or {},
    )

