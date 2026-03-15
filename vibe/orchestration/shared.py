from __future__ import annotations

import fnmatch

REPLAN_HINT_KEYWORDS = (
    "architecture",
    "architect",
    "adr",
    "api",
    "boundary",
    "contract",
    "cross-module",
    "cross module",
    "directory",
    "envspec",
    "interface",
    "module",
    "ownership",
    "plan",
    "replan",
    "route",
    "router",
    "schema",
    "shared_context",
    "shared context",
)

LOW_LEVEL_SCOPE_ERROR_TYPES = {
    "missing_import",
    "missing_export",
    "symbol_rename",
    "wrong_import_path",
    "typing_runtime_issue",
    "syntax_error",
    "config_missing",
    "scope_mismatch",
    "exception_taxonomy_mismatch",
    "engine_interface_mismatch",
    "data_shape_mismatch",
    "contract_drift",
}


class WriteScopeDeniedError(RuntimeError):
    def __init__(self, *, path: str, allow: list[str], deny: list[str]) -> None:
        allow_preview = ", ".join(list(allow or [])[:6])
        deny_preview = ", ".join(list(deny or [])[:6])
        msg = f"Write scope denied: {path}".strip()
        if allow_preview:
            msg = f"{msg} (allow=[{allow_preview}{'…' if len(list(allow or [])) > 6 else ''}])"
        if deny_preview:
            msg = f"{msg} (deny=[{deny_preview}{'…' if len(list(deny or [])) > 6 else ''}])"
        super().__init__(msg)
        self.path = path
        self.allow = list(allow or [])
        self.deny = list(deny or [])


def _normalize_scope_pattern(pat: str) -> str:
    return (str(pat or "").replace("\\", "/").strip()).lstrip("/")


def _matches_scope_pattern(rel: str, pat: str) -> bool:
    r = _normalize_scope_pattern(rel).lower()
    p = _normalize_scope_pattern(pat).lower()
    if not p:
        return False
    if any(ch in p for ch in ["*", "?", "["]):
        def variants(pattern: str) -> list[str]:
            # Interpret `**/` as "zero or more directories" (like globstar).
            # Python's fnmatch doesn't treat `**` specially, and `**/*.py` would not match `base.py`.
            seen: set[str] = set()
            queue: list[str] = [pattern]
            out: list[str] = []
            while queue:
                q = queue.pop(0)
                if q in seen:
                    continue
                seen.add(q)
                out.append(q)
                if "/**/" in q:
                    queue.append(q.replace("/**/", "/", 1))
                if q.startswith("**/"):
                    queue.append(q[3:])
                if q.endswith("/**"):
                    queue.append(q[: -3].rstrip("/"))
            return out

        try:
            return any(fnmatch.fnmatch(r, v) for v in variants(p)[:32])
        except Exception:
            return False
    p2 = p.rstrip("/")
    if not p2:
        return False
    return r == p2 or r.startswith(p2 + "/")


def _in_write_scope(rel: str, *, allow: list[str], deny: list[str]) -> bool:
    r = _normalize_scope_pattern(rel)
    if not r:
        return False
    d = [p for p in (deny or []) if _normalize_scope_pattern(p)]
    a = [p for p in (allow or []) if _normalize_scope_pattern(p)]
    if d and any(_matches_scope_pattern(r, p) for p in d[:200]):
        return False
    if a and not any(_matches_scope_pattern(r, p) for p in a[:400]):
        return False
    return True
