from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Set

from vibe.schemas import packs


@dataclass(frozen=True)
class DeliveryNeeds:
    wants_live_data: bool = False


_LIVE_DATA_HINTS = [
    # Chinese
    "实时",
    "行情",
    "价格",
    "报价",
    "汇率",
    "金价",
    "黄金",
    "股票",
    "币价",
    "比特币",
    "以太坊",
    "天气",
    # English
    "real-time",
    "realtime",
    "live price",
    "quote",
    "rates",
    "exchange rate",
]


def infer_delivery_needs(task_text: str) -> DeliveryNeeds:
    t = (task_text or "").strip()
    low = t.lower()
    wants_live = any(h in t for h in _LIVE_DATA_HINTS if h and not h.isascii()) or any(
        h in low for h in _LIVE_DATA_HINTS if h and h.isascii()
    )
    return DeliveryNeeds(wants_live_data=wants_live)


def _add_unique(items: list[str], value: str) -> None:
    v = (value or "").strip()
    if not v:
        return
    for it in items:
        if (it or "").strip() == v:
            return
    items.append(v)


def _contains_any(items: Iterable[str], needles: Iterable[str]) -> bool:
    hay = "\n".join([str(x or "") for x in items]).lower()
    for n in needles:
        if not n:
            continue
        if n.lower() in hay:
            return True
    return False


def augment_requirement_pack(req: packs.RequirementPack, *, task_text: str) -> packs.RequirementPack:
    """
    Deterministic post-processing to keep the workflow delivery-oriented.
    Avoids relying on the PM model to remember to include "how to run" / "data reality" requirements.
    """

    needs = infer_delivery_needs(task_text)
    out = req.model_copy(deep=True)

    # Always require runnable handoff and verification.
    if not _contains_any(out.acceptance, ["readme", "运行", "启动", "how to run", "getting started"]):
        _add_unique(out.acceptance, "README.md 写清楚：安装依赖、启动服务/前端、以及最小验证步骤（让项目能跑起来）")

    if needs.wants_live_data:
        if not _contains_any(out.acceptance, ["数据源", "source", "mock", "模拟", "真实", "third-party", "第三方", "api"]):
            _add_unique(out.acceptance, "必须明确数据来源：是真实外部数据还是模拟数据；如无法拿到真实数据，必须标注为 mock 并说明如何切换")
        if not _contains_any(out.constraints, ["assume", "假设", "fallback", "回退", "环境变量", "env", "key"]):
            _add_unique(out.constraints, "Assume: 默认接入可配置的真实外部数据源；如缺少 API key/网络不可达则回退 mock，并在接口/UI 标注 source=mock")

    # Keep acceptance bounded.
    out.acceptance = list(out.acceptance or [])[:20]
    out.constraints = list(out.constraints or [])[:20]
    out.non_goals = list(out.non_goals or [])[:20]
    return out


def augment_plan(
    plan: packs.Plan,
    *,
    req: packs.RequirementPack | None,
    task_text: str,
    activated_agents: Set[str],
    max_tasks: int = 5,
) -> packs.Plan:
    """
    Ensure the plan includes delivery-critical items (docs + data source clarity) even if the Router forgets.
    """

    needs = infer_delivery_needs(task_text)
    out = plan.model_copy(deep=True)
    tasks = list(out.tasks or [])

    def has_hint(needles: list[str]) -> bool:
        for t in tasks:
            blob = f"{t.title}\n{t.description}".lower()
            if any(n.lower() in blob for n in needles if n):
                return True
        return False

    # Prefer putting delivery work onto an already-activated coder.
    delivery_agent = "coder_backend"
    if "coder_frontend" in activated_agents and any(k in (task_text or "") for k in ["前端", "UI", "界面", "React", "Vite", "TypeScript", "TSX"]):
        delivery_agent = "coder_frontend"
    if delivery_agent not in activated_agents:
        delivery_agent = "coder_backend"

    cap = max(1, int(max_tasks))

    if not has_hint(["readme", "运行", "启动", "how to run", "getting started"]):
        if len(tasks) < cap:
            tasks.append(
                packs.PlanTask(
                    id="t_delivery_docs",
                    title="交付说明",
                    agent=delivery_agent,
                    description="更新 README.md：安装依赖、启动、最小验证步骤；说明关键配置（含环境变量）。",
                )
            )
        else:
            # Merge into the last task to keep <= cap.
            last = tasks[-1]
            last = last.model_copy(
                update={"description": (last.description or "").rstrip() + "\n\n交付补充：更新 README.md（安装/启动/最小验证步骤；关键配置）。"}
            )
            tasks[-1] = last

    if needs.wants_live_data and not has_hint(["数据源", "api", "第三方", "mock", "模拟", "真实", "price", "quote"]):
        if len(tasks) < cap:
            tasks.append(
                packs.PlanTask(
                    id="t_data_source",
                    title="数据源落地",
                    agent="coder_backend" if "coder_backend" in activated_agents else delivery_agent,
                    description="实现可配置的数据源接入（优先真实外部数据；失败回退 mock，并在接口/UI 标注 source）。",
                )
            )
        else:
            last = tasks[-1]
            last = last.model_copy(
                update={"description": (last.description or "").rstrip() + "\n\n数据源补充：实现可配置真实数据源；失败回退 mock，并标注 source。"}
            )
            tasks[-1] = last

    out.tasks = tasks[:cap]
    return out
