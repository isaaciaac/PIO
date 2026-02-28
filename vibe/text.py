from __future__ import annotations

import locale
from typing import Iterable, Optional


def decode_bytes(data: bytes, *, extra_encodings: Optional[Iterable[str]] = None) -> str:
    """
    Best-effort decoding for subprocess outputs stored as raw bytes.

    Why: On Windows, many tools emit text using the active code page (e.g. GBK/CP936),
    which would look like mojibake if we always decode as UTF-8.
    """
    if not data:
        return ""
    b = bytes(data)

    # BOM / UTF-16 hints
    if b.startswith(b"\xef\xbb\xbf"):
        try:
            return b.decode("utf-8-sig")
        except Exception:
            pass
    if b.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return b.decode("utf-16")
        except Exception:
            pass
    if b.count(b"\x00") > (len(b) // 4):
        for enc in ("utf-16", "utf-16le", "utf-16be"):
            try:
                return b.decode(enc)
            except Exception:
                continue

    # Prefer UTF-8 when possible (stable for artifacts).
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        pass

    candidates: list[str] = []
    pref = locale.getpreferredencoding(False)
    if pref:
        candidates.append(pref)

    # Common Windows / CN encodings.
    candidates.extend(["gbk", "cp936", "gb2312", "big5", "cp950", "cp1252"])
    if extra_encodings:
        for e in extra_encodings:
            e = (e or "").strip()
            if e and e not in candidates:
                candidates.append(e)

    for enc in candidates:
        try:
            return b.decode(enc)
        except Exception:
            continue

    return b.decode("utf-8", errors="replace")

