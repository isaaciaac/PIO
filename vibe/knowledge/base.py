from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

import yaml
from pydantic import BaseModel, Field


class KnowledgeEntry(BaseModel):
    id: str
    title: str
    patterns: List[str] = Field(default_factory=list)
    advice: List[str] = Field(default_factory=list)
    actions: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)


def _kb_path() -> Path:
    # Works for editable installs and wheels with package-data.
    return (Path(__file__).resolve().parent / "solutions.yaml").resolve()


@lru_cache(maxsize=1)
def load_knowledge() -> List[KnowledgeEntry]:
    path = _kb_path()
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []

    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    out: List[KnowledgeEntry] = []
    for it in entries[:200]:
        if not isinstance(it, dict):
            continue
        try:
            out.append(KnowledgeEntry.model_validate(it))
        except Exception:
            continue
    return out


def match_knowledge(text: str, *, limit: int = 3) -> List[Tuple[KnowledgeEntry, float]]:
    """
    Lightweight regex-based matching against the built-in knowledge base.

    Returns a list of (entry, score) sorted by score desc.
    """
    t = (text or "").strip()
    if not t:
        return []
    entries = load_knowledge()
    if not entries:
        return []

    scored: List[Tuple[KnowledgeEntry, float]] = []
    for e in entries:
        pats = [p for p in (e.patterns or []) if isinstance(p, str) and p.strip()]
        if not pats:
            continue
        hits = 0
        for p in pats[:12]:
            try:
                if re.search(p, t, flags=re.IGNORECASE | re.MULTILINE):
                    hits += 1
            except re.error:
                continue
        if hits <= 0:
            continue
        score = hits / max(1.0, min(float(len(pats)), 12.0))
        scored.append((e, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[: max(0, int(limit))]


def best_knowledge_snippet(text: str, *, max_lines: int = 10) -> Optional[str]:
    matches = match_knowledge(text, limit=1)
    if not matches:
        return None
    entry, _score = matches[0]
    lines: List[str] = []
    lines.append(f"已知坑：{entry.id} — {entry.title}".strip())
    for a in list(entry.actions or [])[: max(0, max_lines - 1)]:
        s = str(a).strip()
        if not s:
            continue
        lines.append(f"- {s}"[:240])
        if len(lines) >= max_lines:
            break
    return "\n".join(lines).strip() if lines else None

