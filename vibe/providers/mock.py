from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Type, TypeVar

from pydantic import BaseModel

from vibe.providers.base import ProviderMeta, ProviderResult
from vibe.schemas import packs as schemas


T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class MockProvider:
    provider_id: str = "mock"

    def chat_json(self, *, model: str, messages: List[Dict[str, str]], schema: Type[T], temperature: float = 0.0) -> tuple[T, ProviderResult]:
        _ = (model, temperature)
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = m.get("content") or ""
                break

        if schema is schemas.RequirementPack:
            out: Any = schemas.RequirementPack(
                summary=last_user.strip().splitlines()[0][:120] or "Mock requirement",
                acceptance=["mock: workflow completes", "mock: green checkpoint created"],
                non_goals=["mock: no real code changes"],
                constraints=["VIBE_MOCK_MODE=1"],
            )
        elif schema is schemas.ChatReply:
            client = (os.getenv("VIBE_CLIENT") or "").strip().lower()
            vscode_env = bool(os.getenv("VSCODE_PID") or (os.getenv("TERM_PROGRAM") or "").strip().lower() == "vscode")
            if client == "vscode" or vscode_env:
                actions = [
                    "如果要改代码：切到写项目模式后，继续描述需求；信息足够时会自动执行工作流并落地到代码",
                    "如果只想咨询用法：继续在聊天模式提问即可",
                ]
            else:
                actions = [
                    "如果要改代码：执行 `vibe task add \"...\"` 然后 `vibe run`",
                    "如果只想咨询用法：继续在聊天模式提问即可",
                ]
            out = schemas.ChatReply(
                reply=(
                    "（mock）我是 Vibe 的产品经理（PM）代理：我可以帮你澄清需求、给出验收标准（AC）、"
                    "列出约束/非目标，并指导你如何用 `vibe` 在当前项目里跑起多代理工作流。\n\n"
                    f"你刚刚说：{last_user.strip()[:200]}"
                ),
                suggested_actions=actions,
                pointers=[],
            )
        elif schema is schemas.Plan:
            out = schemas.Plan(
                tasks=[
                    schemas.PlanTask(id="t1", title="Spec", agent="pm", description="Produce RequirementPack"),
                    schemas.PlanTask(id="t2", title="Implement", agent="coder_backend", description="Produce CodeChange"),
                    schemas.PlanTask(id="t3", title="Test", agent="qa", description="Run tests and report"),
                ]
            )
        elif schema is schemas.CodeChange:
            if os.getenv("VIBE_MOCK_WRITES", "").strip() == "1":
                readme = (
                    "# Mock Project (from Vibe)\n\n"
                    "这是一个用 mock 模式生成的最小 Python 项目骨架，用来验证 Vibe 的「从 0 写文件」能力。\n\n"
                    "## 运行\n\n"
                    "```bash\n"
                    "python main.py\n"
                    "```\n\n"
                    "## 测试（unittest）\n\n"
                    "```bash\n"
                    "python -m unittest -q\n"
                    "```\n"
                )
                out = schemas.CodeChange(
                    kind="patch",
                    summary="mock: scaffolded a minimal python project",
                    writes=[
                        schemas.FileWrite(path="hello.txt", content="hello from mock\n"),
                        schemas.FileWrite(
                            path="main.py",
                            content=(
                                "def greet(name: str = \"world\") -> str:\n"
                                "    return f\"hello, {name}\"\n\n"
                                "if __name__ == \"__main__\":\n"
                                "    print(greet())\n"
                            ),
                        ),
                        schemas.FileWrite(path="README.md", content=readme),
                        schemas.FileWrite(
                            path="tests/test_main.py",
                            content=(
                                "import unittest\n\n"
                                "from main import greet\n\n\n"
                                "class TestGreet(unittest.TestCase):\n"
                                "    def test_default(self):\n"
                                "        self.assertEqual(greet(), \"hello, world\")\n\n"
                                "    def test_name(self):\n"
                                "        self.assertEqual(greet(\"vibe\"), \"hello, vibe\")\n"
                            ),
                        ),
                    ],
                    files_changed=["hello.txt", "main.py", "README.md", "tests/test_main.py"],
                )
            else:
                out = schemas.CodeChange(kind="noop", summary="mock: no changes", files_changed=[])
        elif schema is schemas.TestReport:
            out = schemas.TestReport(
                commands=["mock"],
                results=[schemas.TestResult(command="mock", returncode=0, passed=True, stdout="", stderr="")],
                passed=True,
                blockers=[],
            )
        elif schema is schemas.ContextPacket:
            out = schemas.ContextPacket(repo_pointers=[], log_pointers=[], constraints=[], acceptance=[], recent_events=[])
        elif schema is schemas.LogIndex:
            out = schemas.LogIndex(items=[])
        elif schema is schemas.ReferenceItem:
            out = schemas.ReferenceItem(id="ref_mock", title="mock ref", tags=["mock"], content="mock content", source="mock")
        elif schema is schemas.UseCasePack:
            out = schemas.UseCasePack(positive=["mock ok"], negative=["mock fail"], edge_cases=["mock edge"])
        elif schema is schemas.UXCopyPack:
            out = schemas.UXCopyPack(strings={})
        elif schema is schemas.DecisionPack:
            out = schemas.DecisionPack(adrs=[])
        elif schema is schemas.ContractPack:
            out = schemas.ContractPack(contracts=[])
        elif schema is schemas.MigrationPlan:
            out = schemas.MigrationPlan(steps=[], rollback_steps=[])
        elif schema is schemas.EnvSpec:
            out = schemas.EnvSpec(commands=[])
        elif schema is schemas.CIPack:
            out = schemas.CIPack(notes=[])
        elif schema is schemas.ReleasePack:
            out = schemas.ReleasePack(version="0.0.0", changelog=[])
        elif schema is schemas.ReviewReport:
            out = schemas.ReviewReport(passed=True, blockers=[], nits=[], pointers=[])
        elif schema is schemas.RiskRegister:
            out = schemas.RiskRegister(passed=True, blockers=[], highs=[])
        elif schema is schemas.PerfReport:
            out = schemas.PerfReport(notes=[])
        elif schema is schemas.ComplianceReport:
            out = schemas.ComplianceReport(passed=True, notes=[])
        elif schema is schemas.DocPack:
            out = schemas.DocPack(files=[])
        elif schema is schemas.RunbookPack:
            out = schemas.RunbookPack(sections=[])
        else:
            out = schema.model_validate({})

        meta = ProviderMeta(provider=self.provider_id, model="mock", usage={})
        return out, ProviderResult(raw_text=out.model_dump_json(), meta=meta)
