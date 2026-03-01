from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field


MemoryKind = Literal[
    "chat_digest",
    "lesson",
    "incident",
    "strategy",
    "user_hint",
    "postmortem",
]


class ChatDigest(BaseModel):
    summary: str
    pinned: List[str] = Field(default_factory=list)
    background: List[str] = Field(default_factory=list)
    open_questions: List[str] = Field(default_factory=list)


class MemoryRecord(BaseModel):
    ts: str
    agent_id: str
    kind: MemoryKind = "chat_digest"
    digest: ChatDigest
    pointers: List[str] = Field(default_factory=list)
