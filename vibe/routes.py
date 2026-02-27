from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from vibe.schemas.packs import RouteDecision, RouteLevel


_ROUTE_RANK: dict[RouteLevel, int] = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}


def _max_level(a: RouteLevel, b: RouteLevel) -> RouteLevel:
    return a if _ROUTE_RANK[a] >= _ROUTE_RANK[b] else b


@dataclass(frozen=True)
class DiffStats:
    file_count: int = 0
    loc_added: int = 0
    loc_deleted: int = 0
    paths: tuple[str, ...] = ()
    pointer: Optional[str] = None

    @property
    def loc_changed(self) -> int:
        return int(self.loc_added) + int(self.loc_deleted)


@dataclass(frozen=True)
class RiskSignals:
    touches_auth: bool = False
    touches_crypto: bool = False
    touches_external_api: bool = False
    touches_migration: bool = False
    touches_release: bool = False
    touches_compliance: bool = False
    cross_module: bool = False
    contract_change: bool = False
    needs_repro_env: bool = False


def _has_any(text: str, patterns: Iterable[str]) -> bool:
    for p in patterns:
        if p and p in text:
            return True
    return False


def _paths_have_any(paths: Iterable[str], patterns: Iterable[str]) -> bool:
    pats = [p for p in patterns if p]
    if not pats:
        return False
    for path in paths:
        lp = path.lower()
        for p in pats:
            if p in lp:
                return True
    return False


def detect_risks(task_text: str, *, diff: DiffStats) -> RiskSignals:
    text = task_text.strip()
    text_l = text.lower()
    paths = diff.paths

    touches_auth = _has_any(
        text_l,
        [
            "auth",
            "oauth",
            "jwt",
            "login",
            "rbac",
            "acl",
            "sso",
            "permission",
        ],
    ) or _has_any(text, ["鉴权", "认证", "授权", "权限", "登录", "令牌", "会话"])

    touches_crypto = _has_any(text_l, ["encrypt", "encryption", "crypto", "tls", "ssl", "hmac", "rsa", "aes"]) or _has_any(
        text, ["加密", "证书", "密钥", "签名", "脱敏"]
    )

    touches_external_api = _has_any(text_l, ["openapi", "swagger", "api", "grpc", "http", "webhook"]) or _has_any(
        text, ["接口", "外部", "第三方", "对接", "回调", "契约", "协议"]
    )

    contract_change = touches_external_api and (
        _has_any(text_l, ["schema", "contract", "breaking", "response", "request", "error code", "protobuf"])
        or _has_any(text, ["变更", "兼容", "错误码", "字段", "入参", "出参", "版本"])
    )

    touches_migration = _has_any(
        text_l,
        ["migration", "migrate", "alembic", "flyway", "liquibase", "ddl", "schema change"],
    ) or _has_any(text, ["数据库", "迁移", "表结构", "字段", "索引", "回滚脚本"])

    touches_release = _has_any(text_l, ["release", "changelog", "tag", "version", "rollback", "hotfix"]) or _has_any(
        text, ["发布", "上线", "版本", "回滚", "热修", "变更说明"]
    )

    needs_repro_env = _has_any(text_l, ["reproducible", "devcontainer", "docker", "compose", "ci", "cd"]) or _has_any(
        text, ["可复现", "环境", "容器", "流水线", "CI", "CD"]
    )

    touches_compliance = _has_any(text_l, ["gdpr", "privacy", "compliance", "pii"]) or _has_any(
        text, ["合规", "隐私", "个人信息", "审计", "数据出境"]
    )

    # Path-based hints (works when repo is dirty before running)
    if paths:
        touches_auth = touches_auth or _paths_have_any(paths, ["auth", "oauth", "jwt", "rbac", "acl", "permissions"])
        touches_crypto = touches_crypto or _paths_have_any(paths, ["crypto", "tls", "ssl", "cert", "secrets"])
        touches_external_api = touches_external_api or _paths_have_any(paths, ["openapi", "swagger", "api", "proto", "contracts"])
        touches_migration = touches_migration or _paths_have_any(paths, ["migrations", "alembic", "schema", "ddl"])
        touches_release = touches_release or _paths_have_any(paths, [".github/workflows", "ci", "cd", "release", "changelog"])
        needs_repro_env = needs_repro_env or _paths_have_any(paths, ["dockerfile", "compose", ".devcontainer", "makefile"])
        touches_compliance = touches_compliance or _paths_have_any(paths, ["privacy", "compliance", "gdpr"])
        contract_change = contract_change or _paths_have_any(paths, ["openapi", "swagger", "proto", "schema", "contracts"])

    # Cross-module heuristic: lots of files/LOC or spread across multiple top-level dirs
    top_levels = {p.replace("\\", "/").split("/", 1)[0] for p in paths if p}
    cross_module = False
    if len(top_levels) >= 3 and diff.file_count >= 6:
        cross_module = True
    if diff.file_count >= 12 or diff.loc_changed >= 800:
        cross_module = True

    return RiskSignals(
        touches_auth=touches_auth,
        touches_crypto=touches_crypto,
        touches_external_api=touches_external_api,
        touches_migration=touches_migration,
        touches_release=touches_release,
        touches_compliance=touches_compliance,
        cross_module=cross_module,
        contract_change=contract_change,
        needs_repro_env=needs_repro_env,
    )


def _normalize_requested(level: Optional[str]) -> Optional[RouteLevel]:
    if not level:
        return None
    lv = level.strip().upper()
    if lv in {"AUTO", "DEFAULT"}:
        return None
    if lv in {"L0", "L1", "L2", "L3", "L4"}:
        return lv  # type: ignore[return-value]
    return None


def decide_route(
    *,
    task_text: str,
    diff: DiffStats,
    recent_test_fail_count: int = 0,
    requested_level: Optional[str] = None,
) -> RouteDecision:
    requested = _normalize_requested(requested_level)
    risks = detect_risks(task_text, diff=diff)

    hard_required: RouteLevel = "L0"
    reasons: list[str] = []

    if risks.touches_migration:
        hard_required = _max_level(hard_required, "L4")
        reasons.append("涉及数据迁移/数据库结构变更：最低需要 L4（迁移计划/回滚/审计）")
    if risks.touches_compliance:
        hard_required = _max_level(hard_required, "L4")
        reasons.append("涉及合规/隐私/审计域：最低需要 L4（合规门禁）")

    if risks.touches_release or risks.needs_repro_env:
        hard_required = _max_level(hard_required, "L3")
        reasons.append("涉及发布/可复现环境：最低需要 L3（EnvSpec/安全/文档/交付）")

    if (
        risks.cross_module
        or risks.contract_change
        or risks.touches_external_api
        or risks.touches_auth
        or risks.touches_crypto
    ):
        hard_required = _max_level(hard_required, "L2")
        reasons.append("跨模块/契约/鉴权/加密/外部API等风险域：最低需要 L2（ADR-lite/审查/集成测试）")

    # Diff-size heuristic (only meaningful when repo already has local diffs)
    if diff.file_count >= 20 or diff.loc_changed >= 2000:
        hard_required = _max_level(hard_required, "L2")
        reasons.append(f"检测到较大改动（files={diff.file_count}, loc={diff.loc_changed}）：最低需要 L2")

    if recent_test_fail_count > 0:
        reasons.append(f"近期测试失败次数：{recent_test_fail_count}（将更严格记录 blockers）")

    # If nothing forces L2+, default to L1 unless user explicitly asks for L0.
    if requested is None:
        if hard_required == "L0":
            return RouteDecision(route_level="L1", reasons=["默认：低风险任务走 L1 标准路径"] + reasons)
        return RouteDecision(route_level=hard_required, reasons=reasons or [f"硬规则选择：{hard_required}"])

    # User requested a level; do not allow downgrading below hard rules.
    if _ROUTE_RANK[requested] < _ROUTE_RANK[hard_required]:
        reasons.insert(0, f"用户指定 {requested} 低于硬规则最低要求 {hard_required}，已升级到 {hard_required}")
        return RouteDecision(route_level=hard_required, reasons=reasons)

    # Explicit L0 is allowed only when hard_required is L0 (i.e. no forced gates).
    if requested == "L0" and hard_required == "L0":
        reasons.insert(0, "用户明确指定 L0 极速路径（仅 smoke 验证，检查点不标绿）")
        return RouteDecision(route_level="L0", reasons=reasons)

    # Otherwise honor the requested level (can be >= hard_required).
    if requested != hard_required:
        reasons.insert(0, f"用户指定路由等级：{requested}")
    return RouteDecision(route_level=requested, reasons=reasons)


_ROUTE_TOKEN_RE = re.compile(r"\bL[0-4]\b", re.IGNORECASE)


def extract_explicit_route_hint(task_text: str) -> Optional[RouteLevel]:
    """
    Best-effort extraction of 'L0..L4' hints from task text. This is NOT a hard
    override; the CLI/UI option should be preferred.
    """

    m = _ROUTE_TOKEN_RE.search(task_text or "")
    if not m:
        return None
    token = m.group(0).upper()
    if token in {"L0", "L1", "L2", "L3", "L4"}:
        return token  # type: ignore[return-value]
    return None

