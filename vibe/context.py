from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from vibe.config import AgentContextConfig, VibeConfig
from vibe.schemas.memory import ChatDigest, MemoryRecord
from vibe.storage.artifacts import ArtifactsStore


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ChatLine:
    ts: str
    role: str
    content: str


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def read_chat_lines(path: Path) -> list[ChatLine]:
    items = _read_jsonl(path)
    out: list[ChatLine] = []
    for it in items:
        ts = str(it.get("ts") or "").strip() or _now_iso()
        role = str(it.get("role") or "").strip()
        content = str(it.get("content") or "").strip()
        if role in {"user", "assistant", "system"} and content:
            out.append(ChatLine(ts=ts, role=role, content=content))
    return out


def write_chat_lines(path: Path, lines: Iterable[ChatLine]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = []
    for ln in lines:
        payload.append({"ts": ln.ts, "role": ln.role, "content": ln.content})
    text = "\n".join(json.dumps(x, ensure_ascii=False) for x in payload) + ("\n" if payload else "")
    path.write_text(text, encoding="utf-8")


def append_memory_record(path: Path, record: MemoryRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record.model_dump(), ensure_ascii=False) + "\n")


def read_memory_records(path: Path, *, limit: int) -> list[MemoryRecord]:
    items = _read_jsonl(path)
    out: list[MemoryRecord] = []
    for it in items[-max(0, limit) :]:
        try:
            out.append(MemoryRecord.model_validate(it))
        except Exception:
            continue
    return out


def effective_context_config(cfg: VibeConfig, *, agent_id: str) -> AgentContextConfig:
    return cfg.context.agents.get(agent_id) or cfg.context.defaults


def estimate_chars(messages: Iterable[dict[str, str]]) -> int:
    total = 0
    for m in messages:
        total += len(str(m.get("content") or ""))
    return total


def _split_text(text: str, *, chunk_chars: int) -> list[str]:
    if chunk_chars <= 0:
        return [text]
    out: list[str] = []
    i = 0
    while i < len(text):
        out.append(text[i : i + chunk_chars])
        i += chunk_chars
    return out


_PIN_KEYWORDS = [
    "必须",
    "不允许",
    "不要",
    "不能",
    "验收",
    "AC",
    "约束",
    "范围",
    "非目标",
    "优先级",
    "安全",
    "权限",
    "鉴权",
    "加密",
    "发布",
]


def _heuristic_digest(text: str, *, pinned_max: int, background_max: int) -> ChatDigest:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    pinned: list[str] = []
    background: list[str] = []
    for l in lines:
        if len(pinned) < pinned_max and any(k.lower() in l.lower() for k in _PIN_KEYWORDS):
            pinned.append(l[:200])
        elif len(background) < background_max:
            background.append(l[:200])
        if len(pinned) >= pinned_max and len(background) >= background_max:
            break
    summary = pinned[0] if pinned else (lines[0][:200] if lines else "（无内容）")
    return ChatDigest(summary=summary, pinned=pinned, background=background, open_questions=[])


def _format_archive_text(lines: list[ChatLine]) -> str:
    parts: list[str] = []
    for ln in lines:
        role = ln.role
        parts.append(f"[{ln.ts}] {role}: {ln.content}")
    return "\n\n".join(parts).strip() + ("\n" if parts else "")


def maybe_compress_chat_history(
    *,
    repo_root: Path,
    agent_id: str,
    cfg: VibeConfig,
    hist_path: Path,
    memory_path: Path,
    incoming_user_message: str,
    history_limit: int,
    digest_builder: Optional[callable] = None,
) -> None:
    """
    Budget guard for long-running chat:
    - archives older chat lines into content-addressed artifacts (may be split into parts)
    - writes a structured digest into `.vibe/views/<agent>/memory.jsonl`
    - rewrites chat.jsonl to keep only a short tail + a small pinned-summary system message

    digest_builder: optional callable(text)->ChatDigest for real model-based digesting (per agent).
    """

    ctx = effective_context_config(cfg, agent_id=agent_id)
    max_chars = max(2000, int(ctx.max_chars))
    trigger_chars = max(1000, int(max_chars * float(ctx.compress_trigger_ratio)))

    keep_last = max(0, int(ctx.keep_last_messages))
    pinned_max = max(0, int(ctx.pinned_max_items))
    archive_chunk_chars = max(2000, int(ctx.archive_chunk_chars))

    lines = read_chat_lines(hist_path)
    if not lines:
        return

    # Quick heuristic: estimate the chat context we'd send (history tail + incoming).
    tail = lines[-max(0, min(history_limit, 64)) :]
    est = sum(len(x.content) for x in tail) + len(incoming_user_message)
    if est <= trigger_chars and (hist_path.stat().st_size if hist_path.exists() else 0) <= 2_000_000:
        return

    if keep_last >= len(lines):
        # Nothing to archive; but we can still avoid oversized context by truncating tail.
        if est <= max_chars:
            return

    archive = lines[: max(0, len(lines) - keep_last)]
    keep = lines[max(0, len(lines) - keep_last) :]
    if not archive:
        return

    artifacts = ArtifactsStore(repo_root)
    archive_text = _format_archive_text(archive)
    archive_parts = _split_text(archive_text, chunk_chars=archive_chunk_chars)
    pointers: list[str] = []
    for idx, part in enumerate(archive_parts):
        suffix = f".chat.part{idx+1}.txt" if len(archive_parts) > 1 else ".chat.txt"
        ptr = artifacts.put_text(part, suffix=suffix, kind="chat_archive").to_pointer()
        pointers.append(ptr)

    # Build digest (prefer model-based builder when not in mock mode).
    digest: ChatDigest
    if os.getenv("VIBE_MOCK_MODE", "").strip() == "1" or digest_builder is None:
        digest = _heuristic_digest(archive_text, pinned_max=pinned_max, background_max=12)
    else:
        try:
            digest = digest_builder(archive_text)
        except Exception:
            digest = _heuristic_digest(archive_text, pinned_max=pinned_max, background_max=12)

    digest.pinned = [s[:200] for s in digest.pinned[:pinned_max]]
    digest.background = [s[:200] for s in digest.background[:12]]
    digest.open_questions = [s[:200] for s in digest.open_questions[:8]]

    append_memory_record(
        memory_path,
        MemoryRecord(ts=_now_iso(), agent_id=agent_id, kind="chat_digest", digest=digest, pointers=pointers),
    )

    pinned_lines = digest.pinned[: min(6, len(digest.pinned))]
    pinned_text = "\n".join([f"- {x}" for x in pinned_lines]).strip()
    sys_summary = (
        "（系统已自动压缩较早对话到 artifacts）\n"
        f"pointers: {', '.join(pointers[:3])}{' …' if len(pointers) > 3 else ''}\n"
    )
    if pinned_text:
        sys_summary += "要点（不可丢失）：\n" + pinned_text + "\n"
    sys_summary += "（事实以 pointers 展开为准）"

    write_chat_lines(
        hist_path,
        [ChatLine(ts=_now_iso(), role="system", content=sys_summary), *keep],
    )

