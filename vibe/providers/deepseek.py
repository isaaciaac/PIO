from __future__ import annotations

from typing import Dict, List

from vibe.providers.base import OpenAICompatProvider, ensure_deepseek_reasoner_format


class DeepSeekProvider(OpenAICompatProvider):
    def __init__(self, *, base_url: str = "https://api.deepseek.com/v1", api_key_env: str = "DEEPSEEK_API_KEY") -> None:
        super().__init__(provider_id="deepseek", base_url=base_url, api_key_env=api_key_env)

    def normalize_messages(self, messages: List[Dict[str, str]], *, model: str) -> List[Dict[str, str]]:
        if model == "deepseek-reasoner":
            return ensure_deepseek_reasoner_format(messages)
        return messages
