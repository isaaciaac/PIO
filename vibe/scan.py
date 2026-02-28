from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


_IGNORE_DIRS = {".git", ".vibe", "__pycache__", ".venv", "venv", "node_modules", "dist", "build", ".pytest_cache"}

_DEFAULT_MAX_FILES = 8000
_DEFAULT_MAX_FILE_BYTES = 256_000
_DEFAULT_MAX_TOTAL_HASH_BYTES = 12_000_000

_SENSITIVE_GLOBS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*id_rsa*",
    "*id_ed25519*",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_relpath(repo_root: Path, path: Path) -> Optional[str]:
    try:
        rel = path.resolve().relative_to(repo_root.resolve()).as_posix()
    except Exception:
        return None
    if rel.startswith("../") or rel.startswith("..\\") or rel == "..":
        return None
    return rel


def _is_sensitive_path(rel: str) -> bool:
    name = rel.split("/")[-1]
    for g in _SENSITIVE_GLOBS:
        if fnmatch.fnmatch(name, g) or fnmatch.fnmatch(rel, g):
            return True
    return False


def _walk_files(repo_root: Path) -> Iterable[Path]:
    ignore = {d.lower() for d in _IGNORE_DIRS}
    for root, dirs, files in os.walk(repo_root):
        # Prune ignored dirs anywhere in the tree (e.g. client/node_modules).
        dirs[:] = [d for d in dirs if d.lower() not in ignore]
        for name in files:
            path = Path(root) / name
            rel = _safe_relpath(repo_root, path)
            if not rel:
                continue
            parts = rel.split("/")
            if any(p.lower() in ignore for p in parts[:-1]):
                continue
            yield path


def _looks_binary(sample: bytes) -> bool:
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    # Heuristic: lots of non-text bytes in the first chunk.
    textish = b"\n\r\t\b\f" + bytes(range(32, 127))
    non = sum(1 for b in sample if b not in textish)
    return non / max(1, len(sample)) > 0.30


def _read_text_best_effort(path: Path, *, max_chars: int = 20000) -> str:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    if len(raw) <= max_chars:
        return raw
    return raw[:max_chars] + "\n…\n"


def _detect_package_manager(repo_root: Path, pkg_dir: Path) -> str:
    # Prefer lockfiles closest to the package.json dir, then repo root.
    for d in [pkg_dir, Path(".")]:
        if (repo_root / d / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (repo_root / d / "yarn.lock").exists():
            return "yarn"
        if (repo_root / d / "package-lock.json").exists():
            return "npm"
    return "npm"


def _collect_node_projects(repo_root: Path, file_rels: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    pkg_paths = [p for p in file_rels if p.endswith("package.json")]
    for rel in sorted(set(pkg_paths))[:24]:
        if _is_sensitive_path(rel):
            continue
        pkg_path = repo_root / rel
        pkg_dir = Path(rel).parent
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            pkg = {}
        scripts = list((pkg.get("scripts") or {}).keys())
        name = str(pkg.get("name") or "").strip() or None
        pm = _detect_package_manager(repo_root, pkg_dir)
        out.append(
            {
                "dir": pkg_dir.as_posix() if str(pkg_dir) != "." else ".",
                "package_json": rel,
                "name": name,
                "package_manager": pm,
                "scripts": scripts[:40],
            }
        )
    return out


def _git_dir(repo_root: Path) -> Optional[Path]:
    git_path = repo_root / ".git"
    if git_path.is_dir():
        return git_path
    if git_path.is_file():
        try:
            text = git_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if not text.lower().startswith("gitdir:"):
            return None
        raw = text.split(":", 1)[1].strip()
        gd = Path(raw)
        if not gd.is_absolute():
            gd = (repo_root / gd).resolve()
        return gd if gd.exists() else None
    return None


def _read_git_ref(git_dir: Path, ref: str) -> Optional[str]:
    ref = ref.strip().lstrip("/")
    p = (git_dir / ref).resolve()
    try:
        if p.exists():
            v = p.read_text(encoding="utf-8", errors="replace").strip()
            if len(v) >= 40 and all(c in "0123456789abcdef" for c in v[:40].lower()):
                return v[:40]
    except OSError:
        return None

    # Fallback to packed-refs
    packed = git_dir / "packed-refs"
    try:
        if not packed.exists():
            return None
        for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("^"):
                continue
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                sha = parts[0].strip()
                if len(sha) >= 40 and all(c in "0123456789abcdef" for c in sha[:40].lower()):
                    return sha[:40]
    except OSError:
        return None
    return None


def git_head_commit(repo_root: Path) -> Optional[str]:
    gd = _git_dir(repo_root)
    if not gd:
        return None
    head = gd / "HEAD"
    try:
        text = head.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if text.lower().startswith("ref:"):
        ref = text.split(":", 1)[1].strip()
        return _read_git_ref(gd, ref)
    if len(text) >= 40 and all(c in "0123456789abcdef" for c in text[:40].lower()):
        return text[:40]
    return None


def _top_level_snapshot(repo_root: Path) -> Tuple[List[str], Dict[str, int]]:
    entries: List[str] = []
    mtimes_ms: Dict[str, int] = {}
    try:
        for child in sorted(repo_root.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))[:200]:
            if child.name in _IGNORE_DIRS:
                continue
            key = child.name + ("/" if child.is_dir() else "")
            entries.append(key)
            try:
                mtimes_ms[key] = int(child.stat().st_mtime * 1000)
            except OSError:
                continue
    except Exception:
        return [], {}
    return entries, mtimes_ms


def _top_level_entries(repo_root: Path) -> List[str]:
    entries, _ = _top_level_snapshot(repo_root)
    return entries


def scan_repo(
    repo_root: Path,
    *,
    max_files: int = _DEFAULT_MAX_FILES,
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
    max_total_hash_bytes: int = _DEFAULT_MAX_TOTAL_HASH_BYTES,
) -> Dict[str, Any]:
    files: List[Dict[str, Any]] = []
    total_hashed = 0
    truncated = False
    ext_counts: Dict[str, int] = {}
    rels: List[str] = []

    for p in _walk_files(repo_root):
        if len(files) >= max_files:
            truncated = True
            break
        rel = _safe_relpath(repo_root, p)
        if not rel:
            continue
        rels.append(rel)
        ext = (p.suffix.lower() or "").strip()
        ext_counts[ext] = ext_counts.get(ext, 0) + 1
        try:
            st = p.stat()
            size = int(st.st_size)
            mtime = float(st.st_mtime)
        except OSError:
            continue

        sha256: Optional[str] = None
        is_text = False
        if (not _is_sensitive_path(rel)) and size <= max_file_bytes and total_hashed + size <= max_total_hash_bytes:
            try:
                data = p.read_bytes()
                is_text = not _looks_binary(data[:2048])
                if is_text:
                    sha256 = hashlib.sha256(data).hexdigest()
                    total_hashed += size
            except OSError:
                sha256 = None
                is_text = False

        files.append(
            {
                "path": rel,
                "size": size,
                "mtime": mtime,
                "ext": ext,
                "sha256": sha256,
                "text": is_text,
                "sensitive": _is_sensitive_path(rel),
            }
        )

    top_level, top_level_mtimes_ms = _top_level_snapshot(repo_root)

    git_head = git_head_commit(repo_root)

    node_projects = _collect_node_projects(repo_root, rels)
    rels_sorted = sorted(set(rels))

    out: Dict[str, Any] = {
        "version": 1,
        "scanned_at": _now_iso(),
        "repo_root": str(repo_root),
        "git_head": git_head,
        "limits": {
            "max_files": max_files,
            "max_file_bytes": max_file_bytes,
            "max_total_hash_bytes": max_total_hash_bytes,
        },
        "truncated": truncated,
        "file_count": len(files),
        "top_level": top_level,
        "top_level_mtimes_ms": top_level_mtimes_ms,
        "ext_counts": ext_counts,
        "node_projects": node_projects,
        "paths_sample": rels_sorted[:400],
    }
    return out


def _scan_state_path(repo_root: Path) -> Path:
    return repo_root / ".vibe" / "manifests" / "scan_state.json"


def scan_is_stale(repo_root: Path, *, max_age_s: int) -> bool:
    state_path = _scan_state_path(repo_root)
    if not state_path.exists():
        return True
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return True
    scanned_at = str(payload.get("scanned_at") or "").strip()
    if not scanned_at:
        return True
    try:
        # Z suffix ISO.
        dt = datetime.fromisoformat(scanned_at.replace("Z", "+00:00"))
        scanned_ts = dt.timestamp()
    except Exception:
        return True

    now_ts = datetime.now(timezone.utc).timestamp()
    if now_ts - scanned_ts > max(0, int(max_age_s)):
        return True

    stored_top = payload.get("top_level")
    stored_mtimes = payload.get("top_level_mtimes_ms")
    if isinstance(stored_top, list) and isinstance(stored_mtimes, dict):
        stored = [str(x) for x in stored_top]
        stored_m = {str(k): int(v) for k, v in stored_mtimes.items() if str(k)}
        current, current_m = _top_level_snapshot(repo_root)
        if stored and current and stored != current:
            return True
        # Directory mtimes help catch added/removed files inside stable top-level dirs.
        for k, v in current_m.items():
            if stored_m.get(k) != v:
                return True
    else:
        # Missing new fields: trigger a one-time refresh after upgrade.
        return True

    stored_head = str(payload.get("git_head") or "").strip()
    current_head = git_head_commit(repo_root) or ""
    if stored_head and current_head and stored_head != current_head:
        return True

    # Key files changed since last scan? (Fast check)
    key_paths = [
        "README.md",
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "Makefile",
        "client/package.json",
        "frontend/package.json",
        "backend/package.json",
        "server/package.json",
    ]
    for rel in key_paths:
        p = repo_root / rel
        if not p.exists():
            continue
        try:
            if p.stat().st_mtime > scanned_ts:
                return True
        except OSError:
            continue

    return False


def write_scan_outputs(repo_root: Path) -> Tuple[Path, Path, Path]:
    """
    Returns: (repo_index_json, repo_overview_md, scan_state_json)
    """
    scan = scan_repo(repo_root)
    mdir = repo_root / ".vibe" / "manifests"
    mdir.mkdir(parents=True, exist_ok=True)

    repo_index = mdir / "repo_index.json"
    repo_index.write_text(json.dumps(scan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # A compact, model-friendly overview (fits into a few hundred lines).
    overview_lines: List[str] = []
    overview_lines.append("# Repo Overview")
    overview_lines.append("")
    overview_lines.append(f"- scanned_at: `{scan.get('scanned_at')}`")
    overview_lines.append(f"- file_count: `{scan.get('file_count')}`" + (" (truncated)" if scan.get("truncated") else ""))
    overview_lines.append("")
    overview_lines.append("## Top-level")
    overview_lines.append("")
    for e in (scan.get("top_level") or [])[:120]:
        overview_lines.append(f"- {e}")
    overview_lines.append("")
    overview_lines.append("## Node projects")
    overview_lines.append("")
    nps = scan.get("node_projects") or []
    if not nps:
        overview_lines.append("- (none detected)")
    else:
        for np in nps[:12]:
            d = str(np.get("dir") or ".")
            pm = str(np.get("package_manager") or "npm")
            pkg = str(np.get("package_json") or "")
            scripts = [str(x) for x in (np.get("scripts") or [])][:12]
            scripts_text = ", ".join(scripts) if scripts else "(no scripts)"
            overview_lines.append(f"- dir: `{d}` ({pm}) · `{pkg}` · scripts: {scripts_text}")
    overview_lines.append("")
    overview_lines.append("## Notes")
    overview_lines.append("")
    overview_lines.append("- 扫描会跳过 `.git/`, `.vibe/`, `node_modules/` 等目录。")
    overview_lines.append("- 可能包含敏感信息的文件（如 `.env`/私钥）只做“存在性”记录，不会计算内容哈希。")
    overview_lines.append("- 模型回答必须基于可追溯的指针/片段；需要更深入时再按需读取相关文件。")
    overview_lines.append("")

    repo_overview = mdir / "repo_overview.md"
    repo_overview.write_text("\n".join(overview_lines), encoding="utf-8")

    scan_state = mdir / "scan_state.json"
    scan_state.write_text(
        json.dumps(
            {
                "version": 1,
                "scanned_at": scan.get("scanned_at"),
                "git_head": scan.get("git_head"),
                "file_count": scan.get("file_count"),
                "truncated": bool(scan.get("truncated")),
                "top_level": scan.get("top_level") or [],
                "top_level_mtimes_ms": scan.get("top_level_mtimes_ms") or {},
                "node_projects": scan.get("node_projects") or [],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    return repo_index, repo_overview, scan_state
