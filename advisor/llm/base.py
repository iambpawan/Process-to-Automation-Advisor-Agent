"""Provider-neutral LLM interface.

Agents talk only to `LLMProvider.chat()`. Each provider (claude.py, ollama.py, ...)
translates these neutral types to and from its own API, so agent logic never
changes when a provider is added.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Literal

from pydantic import BaseModel

Role = Literal["user", "assistant", "tool"]
StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "other"]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    """One conversation entry.

    - user:      `content` is the user's text.
    - assistant: `content` is the visible text; `tool_calls` lists requested calls.
    - tool:      `content` is the result for `tool_call_id`.

    `provider_data` is opaque state a provider may need to replay its own turn
    exactly (e.g. Claude thinking blocks). Other providers ignore it.
    """

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    is_error: bool = False
    provider_data: dict[str, Any] | None = None


class ToolInputError(ValueError):
    """Raised by a tool handler when the model sent invalid arguments."""


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[BaseModel], Any]

    @property
    def parameters(self) -> dict[str, Any]:
        return inline_schema_refs(self.input_model.model_json_schema())


@dataclass
class TextDelta:
    text: str


@dataclass
class ChatResult:
    message: Message
    stop_reason: StopReason
    detail: str | None = None


ChatEvent = TextDelta | ChatResult


class ProviderError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        system: str | None = None,
    ) -> Iterator[ChatEvent]:
        """Stream one model turn: yields TextDelta events, then exactly one ChatResult."""


def inline_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve pydantic's `$defs`/`$ref` so every provider gets a flat JSON schema."""
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(defs[node["$ref"].split("/")[-1]])
            return {k: resolve(v) for k, v in node.items() if k not in ("$defs", "title")}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)
