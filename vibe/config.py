from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field

from vibe.schemas.packs import RouteLevel


class ProviderConfig(BaseModel):
    id: str
    base_url: str
    api_key_env: Optional[str] = None


class AgentMemoryScope(BaseModel):
    view_dir: str
    ledger_read_filter: List[str] = Field(default_factory=list)
    ledger_write_types: List[str] = Field(default_factory=list)
    artifact_read: bool = True
    artifact_write: bool = True


class AgentConfig(BaseModel):
    id: str
    enabled: bool = False
    provider: str
    model: str
    purpose: str
    capabilities: List[str] = Field(default_factory=list)
    io_schema: str
    memory_scope: AgentMemoryScope
    tools_allowed: List[str] = Field(default_factory=list)
    rollback_strategy: str = ""
    prompt_template: str = ""


class PolicyConfig(BaseModel):
    mode: Literal["allow_all", "prompt", "chat_only"] = "allow_all"


class BehaviorConfig(BaseModel):
    # free: fewer questions, more default assumptions and direct proposals
    # balanced: default
    # detailed: more careful, more checks/edge cases
    style: Literal["free", "balanced", "detailed"] = "balanced"

    # How many times the workflow may re-run the internal fix loop when blocked by
    # tests/review failures. Keep this bounded to avoid runaway runs; VS Code UI may
    # also do multi-round retries on top of this.
    fix_loop_max_loops: int = 3


class AgentContextConfig(BaseModel):
    # A lightweight, provider-agnostic budget (char-based heuristic).
    max_chars: int = 16000
    compress_trigger_ratio: float = 0.85
    keep_last_messages: int = 16
    keep_last_digests: int = 3
    pinned_max_items: int = 8
    archive_chunk_chars: int = 20000


class ContextConfig(BaseModel):
    defaults: AgentContextConfig = Field(default_factory=AgentContextConfig)
    agents: Dict[str, AgentContextConfig] = Field(default_factory=dict)


class RouteProfile(BaseModel):
    agents: List[str] = Field(default_factory=list)


class RoutesConfig(BaseModel):
    levels: Dict[RouteLevel, RouteProfile] = Field(default_factory=dict)


class OwnershipRule(BaseModel):
    """
    File ownership / authority rule.

    If a path matches any of `patterns`, only the configured `owners` (and the
    orchestrator router) may directly write the file. Other agents must escalate
    for approval.
    """

    id: str
    description: str = ""
    patterns: List[str] = Field(default_factory=list)
    owners: List[str] = Field(default_factory=list)


class OwnershipConfig(BaseModel):
    enabled: bool = True
    rules: List[OwnershipRule] = Field(default_factory=list)


class GovernanceConfig(BaseModel):
    ownership: OwnershipConfig = Field(default_factory=OwnershipConfig)


class VibeConfig(BaseModel):
    version: str = "0.1"
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    behavior: BehaviorConfig = Field(default_factory=BehaviorConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    routes: RoutesConfig = Field(default_factory=RoutesConfig)
    governance: GovernanceConfig = Field(default_factory=GovernanceConfig)
    providers: Dict[str, ProviderConfig]
    agents: Dict[str, AgentConfig]

    @staticmethod
    def load(path: Path) -> "VibeConfig":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cfg = VibeConfig.model_validate(data)
        if not cfg.routes.levels:
            cfg.routes = default_routes(list(cfg.agents.keys()))
        _migrate_config_in_memory(cfg)
        return cfg

    def redacted(self) -> "VibeConfig":
        # Keys are always in env vars; nothing to redact besides repeating env var name.
        return self


def default_config() -> VibeConfig:
    providers = {
        "deepseek": ProviderConfig(id="deepseek", base_url="https://api.deepseek.com/v1", api_key_env="DEEPSEEK_API_KEY"),
        "dashscope": ProviderConfig(
            id="dashscope",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key_env="DASHSCOPE_API_KEY",
        ),
        "mock": ProviderConfig(id="mock", base_url="mock://", api_key_env=None),
    }

    def agent(
        agent_id: str,
        *,
        enabled: bool,
        provider: str,
        model: str,
        purpose: str,
        capabilities: List[str],
        io_schema: str,
        ledger_write_types: List[str],
        tools_allowed: List[str],
    ) -> AgentConfig:
        return AgentConfig(
            id=agent_id,
            enabled=enabled,
            provider=provider,
            model=model,
            purpose=purpose,
            capabilities=capabilities,
            io_schema=io_schema,
            memory_scope=AgentMemoryScope(
                view_dir=f".vibe/views/{agent_id}/",
                ledger_read_filter=[],
                ledger_write_types=ledger_write_types,
                artifact_read=True,
                artifact_write=True,
            ),
            tools_allowed=tools_allowed,
            rollback_strategy="",
            prompt_template="",
        )

    agents: Dict[str, AgentConfig] = {
        # Orchestration / routing
        "router": agent(
            "router",
            enabled=True,
            provider="dashscope",
            model="qwen-plus",
            purpose="Orchestrate workflow and state transitions",
            capabilities=["orchestration", "routing", "triage"],
            io_schema="vibe.schemas.packs.Plan",
            ledger_write_types=[
                "ROUTE_SELECTED",
                "AGENTS_ACTIVATED",
                "PLAN_CREATED",
                "CONTEXT_PACKET_BUILT",
                "STATE_TRANSITION",
                "INCIDENT_CREATED",
                "ENV_PROBED",
                "OWNERSHIP_CHANGE_REQUESTED",
                "OWNERSHIP_CHANGE_APPROVED",
                "OWNERSHIP_CHANGE_DENIED",
                "CHECKPOINT_CREATED",
                "BRANCH_CREATED",
            ],
            tools_allowed=["read_file", "run_cmd", "git", "search", "write_file", "scan_repo"],
        ),
        "log_compressor": agent(
            "log_compressor",
            enabled=False,
            provider="dashscope",
            model="qwen-flash",
            purpose="Index and compress long logs",
            capabilities=["logs", "compression"],
            io_schema="vibe.schemas.packs.LogIndex",
            ledger_write_types=["LOG_INDEX_BUILT"],
            tools_allowed=["read_artifact"],
        ),
        "researcher": agent(
            "researcher",
            enabled=False,
            provider="dashscope",
            model="qwen-plus",
            purpose="Collect external references into refstore",
            capabilities=["research", "references"],
            io_schema="vibe.schemas.packs.ReferenceItem",
            ledger_write_types=["REF_ADDED", "REF_UPDATED"],
            tools_allowed=["search", "write_refstore"],
        ),
        # Requirements / product
        "pm": agent(
            "pm",
            enabled=True,
            provider="deepseek",
            model="deepseek-reasoner",
            purpose="Define scope and acceptance criteria",
            capabilities=["requirements", "acceptance", "product"],
            io_schema="vibe.schemas.packs.RequirementPack",
            ledger_write_types=["REQ_CREATED", "REQ_UPDATED", "AC_DEFINED"],
            tools_allowed=["read_file", "search"],
        ),
        "intent_expander": agent(
            "intent_expander",
            enabled=True,
            provider="deepseek",
            model="deepseek-reasoner",
            purpose="Expand user intent into a richer, delivery-oriented backlog (level-aware)",
            capabilities=["intent", "product", "scope"],
            io_schema="vibe.schemas.packs.IntentExpansionPack",
            ledger_write_types=["INTENT_EXPANDED"],
            tools_allowed=["read_file", "search", "scan_repo"],
        ),
        "requirements_analyst": agent(
            "requirements_analyst",
            enabled=False,
            provider="deepseek",
            model="deepseek-reasoner",
            purpose="Define use cases and edge cases",
            capabilities=["requirements", "usecases"],
            io_schema="vibe.schemas.packs.UseCasePack",
            ledger_write_types=["USECASES_DEFINED"],
            tools_allowed=["read_file"],
        ),
        "ux_writer": agent(
            "ux_writer",
            enabled=False,
            provider="dashscope",
            model="qwen-plus",
            purpose="Write UX copy and messages",
            capabilities=["ux", "copy"],
            io_schema="vibe.schemas.packs.UXCopyPack",
            ledger_write_types=["UX_COPY_UPDATED"],
            tools_allowed=["read_file", "write_file"],
        ),
        # Architecture / contracts
        "architect": agent(
            "architect",
            enabled=False,
            provider="deepseek",
            model="deepseek-reasoner",
            purpose="Produce ADRs and architecture decisions",
            capabilities=["architecture", "adr"],
            io_schema="vibe.schemas.packs.DecisionPack",
            ledger_write_types=["ADR_ADDED", "ARCH_UPDATED"],
            tools_allowed=["read_file", "write_file"],
        ),
        "api_confirm": agent(
            "api_confirm",
            enabled=False,
            provider="deepseek",
            model="deepseek-reasoner",
            purpose="Confirm API contracts and schemas",
            capabilities=["api", "contract"],
            io_schema="vibe.schemas.packs.ContractPack",
            ledger_write_types=["CONTRACT_CONFIRMED", "CONTRACT_CHANGED"],
            tools_allowed=["read_file", "write_file"],
        ),
        "data_engineer": agent(
            "data_engineer",
            enabled=False,
            provider="dashscope",
            model="qwen3-coder-next",
            purpose="Plan and apply DB migrations",
            capabilities=["db", "migration"],
            io_schema="vibe.schemas.packs.MigrationPlan",
            ledger_write_types=["DB_MIGRATION_PLANNED", "DB_MIGRATION_APPLIED"],
            tools_allowed=["read_file", "write_file", "run_cmd"],
        ),
        # Environment / delivery
        "env_engineer": agent(
            "env_engineer",
            enabled=False,
            provider="dashscope",
            model="qwen-plus",
            purpose="Probe and update environment/run instructions",
            capabilities=["env", "build", "run", "tests", "node", "python"],
            io_schema="vibe.schemas.packs.EnvSpec",
            ledger_write_types=["ENV_PROBED", "ENV_UPDATED"],
            tools_allowed=["run_cmd", "read_file", "write_file"],
        ),
        "devops": agent(
            "devops",
            enabled=False,
            provider="dashscope",
            model="qwen3-coder-next",
            purpose="Maintain CI/CD",
            capabilities=["ci", "cd", "devops"],
            io_schema="vibe.schemas.packs.CIPack",
            ledger_write_types=["CI_UPDATED"],
            tools_allowed=["read_file", "write_file"],
        ),
        "release_manager": agent(
            "release_manager",
            enabled=False,
            provider="dashscope",
            model="qwen-plus",
            purpose="Tag releases and changelog",
            capabilities=["release", "changelog"],
            io_schema="vibe.schemas.packs.ReleasePack",
            ledger_write_types=["RELEASE_TAGGED", "CHANGELOG_UPDATED"],
            tools_allowed=["git", "write_file"],
        ),
        # Implementation
        "coder_backend": agent(
            "coder_backend",
            enabled=True,
            provider="dashscope",
            model="qwen3-coder-next",
            purpose="Implement backend code changes",
            capabilities=["code", "backend", "node", "typescript", "eslint", "debug"],
            io_schema="vibe.schemas.packs.CodeChange",
            ledger_write_types=["CODE_COMMIT", "PATCH_WRITTEN", "CODE_REFACTOR"],
            tools_allowed=["read_file", "write_file", "run_cmd", "git", "search"],
        ),
        "coder_frontend": agent(
            "coder_frontend",
            enabled=False,
            provider="dashscope",
            model="qwen3-coder-next",
            purpose="Implement frontend code changes",
            capabilities=["code", "frontend", "react", "vite", "node", "typescript", "eslint", "debug"],
            io_schema="vibe.schemas.packs.CodeChange",
            ledger_write_types=["CODE_COMMIT", "PATCH_WRITTEN", "CODE_REFACTOR"],
            tools_allowed=["read_file", "write_file", "run_cmd", "git", "search"],
        ),
        "integration_engineer": agent(
            "integration_engineer",
            enabled=False,
            provider="dashscope",
            model="qwen3-coder-next",
            purpose="Integrate modules and align interfaces",
            capabilities=["code", "integration", "contract", "node", "typescript", "debug"],
            io_schema="vibe.schemas.packs.CodeChange",
            ledger_write_types=["CODE_COMMIT", "PATCH_WRITTEN"],
            tools_allowed=["read_file", "write_file", "run_cmd", "git", "search"],
        ),
        "code_reviewer": agent(
            "code_reviewer",
            enabled=False,
            provider="deepseek",
            model="deepseek-reasoner",
            purpose="Review diffs and test results",
            capabilities=["review", "quality"],
            io_schema="vibe.schemas.packs.ReviewReport",
            ledger_write_types=["REVIEW_PASSED", "REVIEW_BLOCKED"],
            tools_allowed=["read_file", "read_artifact", "git"],
        ),
        # Quality / risk
        "qa": agent(
            "qa",
            enabled=True,
            provider="dashscope",
            model="qwen3-coder-next",
            purpose="Run tests and report blockers",
            capabilities=["qa", "tests", "triage", "node", "python"],
            io_schema="vibe.schemas.packs.TestReport",
            ledger_write_types=["TEST_PLAN_CREATED", "TEST_RUN", "TEST_PASSED", "TEST_FAILED"],
            tools_allowed=["run_cmd", "read_artifact", "read_file"],
        ),
        "security": agent(
            "security",
            enabled=False,
            provider="deepseek",
            model="deepseek-reasoner",
            purpose="Threat model and security review",
            capabilities=["security", "threat_model"],
            io_schema="vibe.schemas.packs.RiskRegister",
            ledger_write_types=["SEC_REVIEW_PASSED", "SEC_REVIEW_BLOCKED", "SEC_FINDING"],
            tools_allowed=["read_file", "read_artifact", "search"],
        ),
        "performance": agent(
            "performance",
            enabled=False,
            provider="deepseek",
            model="deepseek-reasoner",
            purpose="Performance analysis and benchmarks",
            capabilities=["performance", "bench"],
            io_schema="vibe.schemas.packs.PerfReport",
            ledger_write_types=["PERF_BENCH_RUN", "PERF_REGRESSION"],
            tools_allowed=["run_cmd", "read_artifact", "read_file"],
        ),
        "compliance": agent(
            "compliance",
            enabled=False,
            provider="deepseek",
            model="deepseek-reasoner",
            purpose="Privacy/compliance review",
            capabilities=["compliance", "privacy"],
            io_schema="vibe.schemas.packs.ComplianceReport",
            ledger_write_types=["COMPLIANCE_BLOCKED", "COMPLIANCE_PASSED"],
            tools_allowed=["read_file"],
        ),
        # Docs / handoff
        "doc_writer": agent(
            "doc_writer",
            enabled=False,
            provider="dashscope",
            model="qwen-plus",
            purpose="Update docs and README",
            capabilities=["docs", "handoff"],
            io_schema="vibe.schemas.packs.DocPack",
            ledger_write_types=["DOC_UPDATED"],
            tools_allowed=["read_file", "write_file"],
        ),
        "support_engineer": agent(
            "support_engineer",
            enabled=False,
            provider="dashscope",
            model="qwen-plus",
            purpose="Create runbooks and support playbooks",
            capabilities=["runbook", "support"],
            io_schema="vibe.schemas.packs.RunbookPack",
            ledger_write_types=["RUNBOOK_UPDATED"],
            tools_allowed=["read_file", "write_file"],
        ),
        "ops_engineer": agent(
            "ops_engineer",
            enabled=False,
            provider="deepseek",
            model="deepseek-reasoner",
            purpose="Reproduce, diagnose, and propose fixes for workflow blockers",
            capabilities=["ops", "triage", "debug"],
            io_schema="vibe.schemas.packs.FixPlanPack",
            ledger_write_types=[],
            tools_allowed=["read_file", "read_artifact", "search", "run_cmd"],
        ),
        "specialist": agent(
            "specialist",
            enabled=False,
            provider="deepseek",
            model="deepseek-reasoner",
            purpose="On-demand specialist for incident triage and missing capability gaps",
            capabilities=["specialist", "debug", "triage"],
            io_schema="vibe.schemas.packs.ChatReply",
            ledger_write_types=[],
            tools_allowed=["read_file", "read_artifact", "search", "run_cmd"],
        ),
    }

    governance = GovernanceConfig(
        ownership=OwnershipConfig(
            enabled=True,
            rules=[
                OwnershipRule(
                    id="contract_and_architecture",
                    description="Core contracts/types and architecture decisions require architect/API confirmation approval.",
                    patterns=[
                        # Domain/contract types (keep narrow; adjust per-repo in vibe.yaml)
                        "src/types.ts",
                        "src/types/**",
                        "**/contracts/**",
                        "**/schemas/**",
                        "openapi.*",
                        "**/*.proto",
                        # Architecture decisions/docs
                        "docs/adr/**",
                    ],
                    owners=["architect", "api_confirm"],
                )
            ],
        )
    )

    return VibeConfig(
        providers=providers,
        agents=agents,
        routes=default_routes(list(agents.keys())),
        governance=governance,
    )


def write_default_config(repo_root: Path, cfg: VibeConfig) -> None:
    cfg_path = repo_root / ".vibe" / "vibe.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(cfg.model_dump(), sort_keys=False, allow_unicode=True), encoding="utf-8")


def default_routes(agent_ids: List[str]) -> RoutesConfig:
    return RoutesConfig(
        levels={
            "L0": RouteProfile(agents=["router", "coder_backend", "qa"]),
            # L1/L2 include env_engineer for on-demand environment probing (invoked only when needed).
            "L1": RouteProfile(agents=["pm", "intent_expander", "router", "coder_backend", "qa", "env_engineer"]),
            "L2": RouteProfile(
                agents=[
                    "pm",
                    "intent_expander",
                    "requirements_analyst",
                    "architect",
                    "api_confirm",
                    "coder_backend",
                    "code_reviewer",
                    "qa",
                    "env_engineer",
                ]
            ),
            "L3": RouteProfile(
                agents=[
                    "pm",
                    "intent_expander",
                    "requirements_analyst",
                    "architect",
                    "api_confirm",
                    "coder_backend",
                    "code_reviewer",
                    "qa",
                    "env_engineer",
                    "devops",
                    "security",
                    "doc_writer",
                    "release_manager",
                ]
            ),
            "L4": RouteProfile(agents=sorted(set(agent_ids))),
        }
    )


def _migrate_config_in_memory(cfg: VibeConfig) -> None:
    # Backward compatible defaults for older vibe.yaml files.
    router = cfg.agents.get("router")
    if router:
        needed = {
            "ROUTE_SELECTED",
            "AGENTS_ACTIVATED",
            "INCIDENT_CREATED",
            "ENV_PROBED",
            "OWNERSHIP_CHANGE_REQUESTED",
            "OWNERSHIP_CHANGE_APPROVED",
            "OWNERSHIP_CHANGE_DENIED",
        }
        existing = set(router.memory_scope.ledger_write_types or [])
        if not needed.issubset(existing):
            router.memory_scope.ledger_write_types = sorted(existing | needed)

        needed_tools = {"scan_repo"}
        existing_tools = set(router.tools_allowed or [])
        if not needed_tools.issubset(existing_tools):
            router.tools_allowed = sorted(existing_tools | needed_tools)

    # Route profiles: older configs may not include env_engineer in L1/L2; we add it only
    # when the profile matches the historical default exactly to avoid surprising custom setups.
    if "env_engineer" in cfg.agents and (cfg.routes.levels or {}):
        l1 = cfg.routes.levels.get("L1")
        if l1 and (l1.agents or []) == ["pm", "router", "coder_backend", "qa"]:
            l1.agents = ["pm", "router", "coder_backend", "qa", "env_engineer"]

        l2 = cfg.routes.levels.get("L2")
        if l2 and (l2.agents or []) == [
            "pm",
            "requirements_analyst",
            "architect",
            "api_confirm",
            "coder_backend",
            "code_reviewer",
            "qa",
        ]:
            l2.agents = [
                "pm",
                "requirements_analyst",
                "architect",
                "api_confirm",
                "coder_backend",
                "code_reviewer",
                "qa",
                "env_engineer",
            ]

    # Capabilities: older configs won't have them; fill minimal defaults without overriding user customizations.
    default_caps: dict[str, list[str]] = {
        "router": ["orchestration", "routing", "triage"],
        "pm": ["requirements", "acceptance", "product"],
        "intent_expander": ["intent", "product", "scope"],
        "requirements_analyst": ["requirements", "usecases"],
        "architect": ["architecture", "adr"],
        "api_confirm": ["api", "contract"],
        "env_engineer": ["env", "build", "run", "tests", "node", "python"],
        "coder_backend": ["code", "backend", "node", "typescript", "eslint", "debug"],
        "coder_frontend": ["code", "frontend", "react", "vite", "node", "typescript", "eslint", "debug"],
        "integration_engineer": ["code", "integration", "contract", "node", "typescript", "debug"],
        "qa": ["qa", "tests", "triage", "node", "python"],
        "code_reviewer": ["review", "quality"],
        "security": ["security", "threat_model"],
        "ops_engineer": ["ops", "triage", "debug"],
        "compliance": ["compliance", "privacy"],
        "performance": ["performance", "bench"],
        "doc_writer": ["docs", "handoff"],
        "release_manager": ["release", "changelog"],
        "devops": ["ci", "cd", "devops"],
        "support_engineer": ["runbook", "support"],
    }
    for aid, caps in default_caps.items():
        a = cfg.agents.get(aid)
        if a is None:
            continue
        if not list(getattr(a, "capabilities", []) or []):
            a.capabilities = list(caps)

    # Add new agents introduced in later versions (non-breaking; disabled by default).
    if "intent_expander" not in cfg.agents:
        try:
            cfg.agents["intent_expander"] = default_config().agents["intent_expander"]
        except Exception:
            pass

    if "specialist" not in cfg.agents:
        try:
            cfg.agents["specialist"] = default_config().agents["specialist"]
        except Exception:
            pass

    if "ops_engineer" not in cfg.agents:
        try:
            cfg.agents["ops_engineer"] = default_config().agents["ops_engineer"]
        except Exception:
            pass

    # Route profiles: older configs won't have intent_expander in L1/L2/L3; add it only when
    # the profile matches historical defaults to avoid surprising custom setups.
    if "intent_expander" in cfg.agents and (cfg.routes.levels or {}):
        l1 = cfg.routes.levels.get("L1")
        if l1 and (l1.agents or []) == ["pm", "router", "coder_backend", "qa", "env_engineer"]:
            l1.agents = ["pm", "intent_expander", "router", "coder_backend", "qa", "env_engineer"]

        l2 = cfg.routes.levels.get("L2")
        if l2 and (l2.agents or []) == [
            "pm",
            "requirements_analyst",
            "architect",
            "api_confirm",
            "coder_backend",
            "code_reviewer",
            "qa",
            "env_engineer",
        ]:
            l2.agents = [
                "pm",
                "intent_expander",
                "requirements_analyst",
                "architect",
                "api_confirm",
                "coder_backend",
                "code_reviewer",
                "qa",
                "env_engineer",
            ]

        l3 = cfg.routes.levels.get("L3")
        if l3 and (l3.agents or []) == [
            "pm",
            "requirements_analyst",
            "architect",
            "api_confirm",
            "coder_backend",
            "code_reviewer",
            "qa",
            "env_engineer",
            "devops",
            "security",
            "doc_writer",
            "release_manager",
        ]:
            l3.agents = [
                "pm",
                "intent_expander",
                "requirements_analyst",
                "architect",
                "api_confirm",
                "coder_backend",
                "code_reviewer",
                "qa",
                "env_engineer",
                "devops",
                "security",
                "doc_writer",
                "release_manager",
            ]

    # Governance/ownership: older configs won't have it; add safe defaults when missing.
    try:
        defaults = default_config().governance.ownership.rules
    except Exception:
        defaults = []
    try:
        ownership = cfg.governance.ownership
    except Exception:
        cfg.governance = default_config().governance
        return
    if ownership.enabled and not list(ownership.rules or []) and defaults:
        ownership.rules = defaults
