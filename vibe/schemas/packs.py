from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


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


class CodeChange(BaseModel):
    kind: Literal["commit", "patch", "noop"]
    summary: str
    commit_hash: Optional[str] = None
    patch_pointer: Optional[str] = None
    files_changed: List[str] = Field(default_factory=list)
    blockers: List[str] = Field(default_factory=list)


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


class RouteDecision(BaseModel):
    route_level: RouteLevel
    reasons: List[str] = Field(default_factory=list)


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
    notes: List[str] = Field(default_factory=list)


class ComplianceReport(BaseModel):
    passed: bool
    notes: List[str] = Field(default_factory=list)


class DocPack(BaseModel):
    files: List[str] = Field(default_factory=list)


class RunbookPack(BaseModel):
    sections: List[str] = Field(default_factory=list)
