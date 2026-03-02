from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


RouteLevel = Literal["L0", "L1", "L2", "L3", "L4"]


class RequirementPack(BaseModel):
    summary: str
    acceptance: List[str] = Field(default_factory=list)
    non_goals: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)


class PlanTask(BaseModel):
    id: str
    title: str
    agent: str
    description: str


class Plan(BaseModel):
    tasks: List[PlanTask] = Field(default_factory=list)


class ContextEventRef(BaseModel):
    id: str
    summary: str
    pointers: List[str] = Field(default_factory=list)


class ContextPacket(BaseModel):
    repo_pointers: List[str] = Field(default_factory=list)
    log_pointers: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    acceptance: List[str] = Field(default_factory=list)
    recent_events: List[ContextEventRef] = Field(default_factory=list)


class FileWrite(BaseModel):
    path: str
    content: str

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data):
        # Models often output {file, content} or {path, text}. Accept common variants.
        if not isinstance(data, dict):
            return data

        out = dict(data)
        if "path" not in out:
            for k in ("file", "filepath", "filename", "name"):
                v = out.get(k)
                if isinstance(v, str) and v.strip():
                    out["path"] = v.strip()
                    break
        if "content" not in out:
            for k in ("text", "contents", "body", "value"):
                v = out.get(k)
                if isinstance(v, str):
                    out["content"] = v
                    break
        return out


class CodeChange(BaseModel):
    kind: Literal["commit", "patch", "noop"]
    summary: str
    commit_hash: Optional[str] = None
    patch_pointer: Optional[str] = None
    writes: List[FileWrite] = Field(default_factory=list)
    files_changed: List[str] = Field(default_factory=list)
    blockers: List[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data):
        if not isinstance(data, dict):
            return data

        out = dict(data)

        # Common field name variants.
        if "kind" not in out and isinstance(out.get("type"), str):
            out["kind"] = out.get("type")
        if "files_changed" not in out:
            for k in ("files", "changed_files", "paths"):
                v = out.get(k)
                if isinstance(v, list):
                    out["files_changed"] = v
                    break
        if "commit_hash" not in out and isinstance(out.get("commit"), str):
            out["commit_hash"] = out.get("commit")
        if "patch_pointer" not in out and isinstance(out.get("patch"), str):
            out["patch_pointer"] = out.get("patch")

        # Normalize kind variants.
        kind = out.get("kind")
        if isinstance(kind, str):
            k = kind.strip().lower()
            mapping = {
                "diff": "patch",
                "patchfile": "patch",
                "commit_hash": "commit",
            }
            if k in mapping:
                out["kind"] = mapping[k]

        return out


class TestResult(BaseModel):
    command: str
    returncode: int
    passed: bool
    stdout: str
    stderr: str
    meta: Optional[str] = None


class TestReport(BaseModel):
    commands: List[str] = Field(default_factory=list)
    results: List[TestResult] = Field(default_factory=list)
    passed: bool
    blockers: List[str] = Field(default_factory=list)
    pointers: List[str] = Field(default_factory=list)


class ChatReply(BaseModel):
    reply: str
    suggested_actions: List[str] = Field(default_factory=list)
    pointers: List[str] = Field(default_factory=list)


class VisionReport(BaseModel):
    summary: str
    description: str = ""
    ocr_text: str = ""
    key_points: List[str] = Field(default_factory=list)
    pointers: List[str] = Field(default_factory=list)


class RouteDecision(BaseModel):
    route_level: RouteLevel
    reasons: List[str] = Field(default_factory=list)


class IncidentPack(BaseModel):
    """
    Deterministic incident capsule produced by the orchestrator when a workflow is blocked.

    This is a *diagnosis/forensics* object: it should stay short, pointer-backed, and actionable.
    """

    source: Literal["tests", "review", "security", "compliance", "performance"]
    category: str
    summary: str
    blocker: str
    evidence_pointers: List[str] = Field(default_factory=list)
    diagnosis: List[str] = Field(default_factory=list)
    next_steps: List[str] = Field(default_factory=list)
    required_capabilities: List[str] = Field(default_factory=list)
    suggested_fix_agent: Optional[str] = None
    autohint: Optional[str] = None


class FixPlanPack(BaseModel):
    """
    Human-readable but structured troubleshooting plan used to guide fix-loop.

    This is produced by an "ops/triage" agent. It MUST stay short and actionable.
    Facts must be pointer-backed (from repo excerpts, artifacts, git refs).
    """

    summary: str
    root_causes: List[str] = Field(default_factory=list)
    repro_steps: List[str] = Field(default_factory=list)
    proposed_fixes: List[str] = Field(default_factory=list)
    files_to_check: List[str] = Field(default_factory=list)
    pointers: List[str] = Field(default_factory=list)


class ReviewReport(BaseModel):
    passed: bool
    blockers: List[str] = Field(default_factory=list)
    nits: List[str] = Field(default_factory=list)
    pointers: List[str] = Field(default_factory=list)


class RiskItem(BaseModel):
    id: str
    severity: Literal["high", "medium", "low"]
    title: str
    description: str
    pointers: List[str] = Field(default_factory=list)


class RiskRegister(BaseModel):
    passed: bool
    blockers: List[RiskItem] = Field(default_factory=list)
    highs: List[RiskItem] = Field(default_factory=list)


# Below are lightweight placeholders for disabled agents in the MVP.
class LogIndex(BaseModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)


class ReferenceItem(BaseModel):
    id: str
    title: str
    tags: List[str] = Field(default_factory=list)
    content: str
    source: Optional[str] = None


class UseCasePack(BaseModel):
    positive: List[str] = Field(default_factory=list)
    negative: List[str] = Field(default_factory=list)
    edge_cases: List[str] = Field(default_factory=list)


class UXCopyPack(BaseModel):
    strings: Dict[str, str] = Field(default_factory=dict)


class DecisionPack(BaseModel):
    adrs: List[Dict[str, Any]] = Field(default_factory=list)
    # Cross-role shared conventions/parameters so downstream agents stay aligned.
    # Keep this concise and stable (ports/dirs/env vars/commands/naming).
    shared_context: Dict[str, Any] = Field(default_factory=dict)


class ContractPack(BaseModel):
    contracts: List[Dict[str, Any]] = Field(default_factory=list)


class MigrationPlan(BaseModel):
    steps: List[str] = Field(default_factory=list)
    rollback_steps: List[str] = Field(default_factory=list)


class EnvSpec(BaseModel):
    commands: List[str] = Field(default_factory=list)


class CIPack(BaseModel):
    notes: List[str] = Field(default_factory=list)


class ReleasePack(BaseModel):
    version: str
    changelog: List[str] = Field(default_factory=list)


class PerfReport(BaseModel):
    passed: bool = True
    blockers: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


class ComplianceReport(BaseModel):
    passed: bool
    notes: List[str] = Field(default_factory=list)


class DocPack(BaseModel):
    files: List[str] = Field(default_factory=list)


class RunbookPack(BaseModel):
    sections: List[str] = Field(default_factory=list)
