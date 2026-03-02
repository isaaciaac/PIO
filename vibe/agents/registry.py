from __future__ import annotations

from typing import Dict, Type

from vibe.agents.base import BaseAgent
from vibe.schemas import packs


class RouterAgent(BaseAgent):
    agent_id = "router"
    output_schema = packs.Plan
    default_provider = "dashscope"
    default_model = "qwen-plus"


class LogCompressorAgent(BaseAgent):
    agent_id = "log_compressor"
    output_schema = packs.LogIndex
    default_provider = "dashscope"
    default_model = "qwen-flash"


class ResearcherAgent(BaseAgent):
    agent_id = "researcher"
    output_schema = packs.ReferenceItem
    default_provider = "dashscope"
    default_model = "qwen-plus"


class PMAgent(BaseAgent):
    agent_id = "pm"
    output_schema = packs.RequirementPack
    default_provider = "deepseek"
    default_model = "deepseek-reasoner"


class RequirementsAnalystAgent(BaseAgent):
    agent_id = "requirements_analyst"
    output_schema = packs.UseCasePack
    default_provider = "deepseek"
    default_model = "deepseek-reasoner"


class UXWriterAgent(BaseAgent):
    agent_id = "ux_writer"
    output_schema = packs.UXCopyPack
    default_provider = "dashscope"
    default_model = "qwen-plus"


class ArchitectAgent(BaseAgent):
    agent_id = "architect"
    output_schema = packs.DecisionPack
    default_provider = "deepseek"
    default_model = "deepseek-reasoner"


class APIConfirmAgent(BaseAgent):
    agent_id = "api_confirm"
    output_schema = packs.ContractPack
    default_provider = "dashscope"
    default_model = "qwen-plus"


class DataEngineerAgent(BaseAgent):
    agent_id = "data_engineer"
    output_schema = packs.MigrationPlan
    default_provider = "dashscope"
    default_model = "qwen-plus"


class EnvEngineerAgent(BaseAgent):
    agent_id = "env_engineer"
    output_schema = packs.EnvSpec
    default_provider = "dashscope"
    default_model = "qwen-turbo"


class DevOpsAgent(BaseAgent):
    agent_id = "devops"
    output_schema = packs.CIPack
    default_provider = "dashscope"
    default_model = "qwen-plus"


class ReleaseManagerAgent(BaseAgent):
    agent_id = "release_manager"
    output_schema = packs.ReleasePack
    default_provider = "dashscope"
    default_model = "qwen-plus"


class CoderBackendAgent(BaseAgent):
    agent_id = "coder_backend"
    output_schema = packs.CodeChange
    default_provider = "dashscope"
    default_model = "qwen3-coder-next"


class CoderFrontendAgent(BaseAgent):
    agent_id = "coder_frontend"
    output_schema = packs.CodeChange
    default_provider = "dashscope"
    default_model = "qwen3-coder-next"


class IntegrationEngineerAgent(BaseAgent):
    agent_id = "integration_engineer"
    output_schema = packs.CodeChange
    default_provider = "dashscope"
    default_model = "qwen3-coder-next"


class CodeReviewerAgent(BaseAgent):
    agent_id = "code_reviewer"
    output_schema = packs.ReviewReport
    default_provider = "deepseek"
    default_model = "deepseek-reasoner"


class QAAgent(BaseAgent):
    agent_id = "qa"
    output_schema = packs.TestReport
    default_provider = "dashscope"
    default_model = "qwen3-coder-flash"


class SecurityAgent(BaseAgent):
    agent_id = "security"
    output_schema = packs.RiskRegister
    default_provider = "deepseek"
    default_model = "deepseek-reasoner"


class PerformanceAgent(BaseAgent):
    agent_id = "performance"
    output_schema = packs.PerfReport
    default_provider = "deepseek"
    default_model = "deepseek-reasoner"


class ComplianceAgent(BaseAgent):
    agent_id = "compliance"
    output_schema = packs.ComplianceReport
    default_provider = "deepseek"
    default_model = "deepseek-reasoner"


class DocWriterAgent(BaseAgent):
    agent_id = "doc_writer"
    output_schema = packs.DocPack
    default_provider = "dashscope"
    default_model = "qwen-plus"


class SupportEngineerAgent(BaseAgent):
    agent_id = "support_engineer"
    output_schema = packs.RunbookPack
    default_provider = "dashscope"
    default_model = "qwen-plus"


class OpsEngineerAgent(BaseAgent):
    agent_id = "ops_engineer"
    output_schema = packs.FixPlanPack
    default_provider = "deepseek"
    default_model = "deepseek-reasoner"


class SpecialistAgent(BaseAgent):
    agent_id = "specialist"
    output_schema = packs.ChatReply
    default_provider = "deepseek"
    default_model = "deepseek-reasoner"


AGENT_REGISTRY: Dict[str, Type[BaseAgent]] = {
    cls.agent_id: cls
    for cls in [
        RouterAgent,
        LogCompressorAgent,
        ResearcherAgent,
        PMAgent,
        RequirementsAnalystAgent,
        UXWriterAgent,
        ArchitectAgent,
        APIConfirmAgent,
        DataEngineerAgent,
        EnvEngineerAgent,
        DevOpsAgent,
        ReleaseManagerAgent,
        CoderBackendAgent,
        CoderFrontendAgent,
        IntegrationEngineerAgent,
        CodeReviewerAgent,
        QAAgent,
        SecurityAgent,
        PerformanceAgent,
        ComplianceAgent,
        DocWriterAgent,
        SupportEngineerAgent,
        OpsEngineerAgent,
        SpecialistAgent,
    ]
}
