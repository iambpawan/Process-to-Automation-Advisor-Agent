"""Ollama provider (local models) — placeholder for a later release.

To implement: POST {base_url}/api/chat with stream=true, `tools` as
[{"type": "function", "function": {"name", "description", "parameters"}}]
(use ToolSpec.parameters), map neutral `tool` messages to role "tool", yield
TextDelta for each streamed chunk, then one ChatResult whose message carries
the returned `tool_calls`. Nothing in advisor/agents needs to change.
"""

from __future__ import annotations

from typing import Iterator

from advisor.config import Settings
from advisor.llm.base import ChatEvent, LLMProvider, Message, ToolSpec


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, settings: Settings):
        self.model = settings.model
        self.base_url = settings.ollama_base_url

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        system: str | None = None,
    ) -> Iterator[ChatEvent]:
        raise NotImplementedError("The Ollama provider is not implemented yet. Set LLM_PROVIDER=claude in .env.")
