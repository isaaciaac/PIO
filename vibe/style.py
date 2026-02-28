from __future__ import annotations

from typing import Literal, Optional


ChatStyle = Literal["free", "balanced", "detailed"]


_ALIASES: dict[str, ChatStyle] = {
    # free / creative
    "free": "free",
    "creative": "free",
    "fast": "free",
    "大胆": "free",
    "自由": "free",
    "自由发挥": "free",
    # balanced
    "balanced": "balanced",
    "default": "balanced",
    "normal": "balanced",
    "平衡": "balanced",
    "默认": "balanced",
    # detailed / strict
    "detailed": "detailed",
    "strict": "detailed",
    "careful": "detailed",
    "谨慎": "detailed",
    "严谨": "detailed",
    "细致": "detailed",
}


def normalize_style(value: Optional[str]) -> ChatStyle:
    raw = (value or "").strip()
    if not raw:
        return "balanced"
    key = raw.lower()
    if key in _ALIASES:
        return _ALIASES[key]
    if raw in _ALIASES:
        return _ALIASES[raw]
    raise ValueError(f"Unknown style: {value!r}. Use: free|balanced|detailed")


def style_temperature(style: ChatStyle) -> float:
    if style == "free":
        return 0.3
    if style == "detailed":
        return 0.15
    return 0.2


def style_prompt(style: ChatStyle) -> str:
    if style == "free":
        return (
            "交互风格：自由发挥（更少追问，更快给方案）。\n"
            "- 先给一个可执行的默认方案；信息缺失时做合理假设并明确写出来\n"
            "- 追问最多 2 个“必须回答”的关键问题（可选问题不要问）\n"
            "- 回复尽量短，但要包含下一步可操作建议"
        )
    if style == "detailed":
        return (
            "交互风格：细致严谨（更全面、更可审计）。\n"
            "- 方案要覆盖边界条件、风险点与验收标准（AC）\n"
            "- 必要时可以追问，但问题控制在 5 个以内，且每个问题都解释“为什么必须问”\n"
            "- 回复可稍长，但要结构化（分点/小标题），避免空泛"
        )
    return (
        "交互风格：平衡（默认）。\n"
        "- 先给方案，再追问 1–3 个关键问题（只问会影响实现/验收的）\n"
        "- 明确下一步建议与可选项"
    )


def style_workflow_hint(style: ChatStyle) -> str:
    if style == "free":
        return (
            "工作流风格：自由发挥。\n"
            "- 信息不足时可做合理默认假设，并在 constraints 里用 'Assume:' 前缀列出\n"
            "- AC 保持少而硬（3–8 条），优先可运行/可验证"
        )
    if style == "detailed":
        return (
            "工作流风格：细致严谨。\n"
            "- AC 更细（8–20 条），覆盖错误场景/权限边界/审计链路\n"
            "- constraints/non_goals 要明确，避免范围蔓延"
        )
    return (
        "工作流风格：平衡。\n"
        "- AC 适中（5–12 条），覆盖核心路径 + 关键错误场景\n"
        "- 信息不足时列出少量 Assumptions（写入 constraints）"
    )

