from __future__ import annotations

from vibe.providers.base import OpenAICompatProvider


class DashScopeProvider(OpenAICompatProvider):
    def __init__(self) -> None:
        super().__init__(
            provider_id="dashscope",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key_env="DASHSCOPE_API_KEY",
        )

