from __future__ import annotations

from vibe.providers.base import OpenAICompatProvider


class DashScopeProvider(OpenAICompatProvider):
    def __init__(
        self,
        *,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env: str = "DASHSCOPE_API_KEY",
    ) -> None:
        super().__init__(
            provider_id="dashscope",
            base_url=base_url,
            api_key_env=api_key_env,
        )
