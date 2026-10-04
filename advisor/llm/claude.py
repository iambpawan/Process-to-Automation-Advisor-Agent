"""Claude API provider (Anthropic Python SDK)."""

from __future__ import annotations

from typing import Any, Iterator

import anthropic

from advisor.config import Settings
from advisor.llm.base import (
    ChatEvent,
    ChatResult,
    LLMProvider,
    Message,
    ProviderError,
    TextDelta,
    ToolCall,
    ToolSpec,
)

# Models that take adaptive thinking + effort and accept fallbacks="default".
_ADAPTIVE_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")
_FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

_STOP_REASONS = {"end_turn": "end_turn", "tool_use": "tool_use", "max_tokens": "max_tokens", "refusal": "refusal"}


class ClaudeProvider(LLMProvider):
    name = "claude"

    def __init__(self, settings: Settings, max_tokens: int = 64000):
        self.model = settings.model
        self.effort = settings.effort
        self.max_tokens = max_tokens
        # api_key=None lets the SDK resolve ANTHROPIC_API_KEY / an `ant auth login` profile.
        self.client = anthropic.Anthropic(api_key=settings.api_key)

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        system: str | None = None,
    ) -> Iterator[ChatEvent]:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": self._to_api_messages(messages),
        }
        if system:
            # Stable system prompt + tool list form a cacheable prefix.
            params["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if tools:
            params["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                    "eager_input_streaming": True,
                }
                for t in tools
            ]
        if self.model.startswith(_ADAPTIVE_MODELS):
            params["thinking"] = {"type": "adaptive"}
            params["output_config"] = {"effort": self.effort}
        if self.model in _FALLBACK_MODELS:
            params["betas"] = [_FALLBACK_BETA]
            params["fallbacks"] = "default"

        try:
            with self.client.beta.messages.stream(**params) as stream:
                for event in stream:
                    if event.type == "text":
                        yield TextDelta(event.text)
                final = stream.get_final_message()
        except ValueError as e:
            # Streamed tool input the SDK could not parse; the turn can be re-issued.
            raise ProviderError(f"Malformed tool input from model: {e}", retryable=True) from e
        except anthropic.AuthenticationError as e:
            raise ProviderError("Claude API key is missing or invalid. Set ANTHROPIC_API_KEY in .env.") from e
        except anthropic.NotFoundError as e:
            raise ProviderError(f"Model '{self.model}' not found. Check LLM_MODEL in .env.") from e
        except anthropic.RateLimitError as e:
            raise ProviderError("Claude API rate limit reached. Wait a moment and retry.", retryable=True) from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"Claude API error {e.status_code}: {e.message}", retryable=e.status_code >= 500) from e
        except anthropic.APIConnectionError as e:
            raise ProviderError("Could not reach the Claude API. Check your network.", retryable=True) from e

        blocks = _after_fallback_boundary(final.content)
        text = "".join(b.text for b in blocks if b.type == "text")
        tool_calls = [ToolCall(id=b.id, name=b.name, arguments=b.input) for b in blocks if b.type == "tool_use"]
        stop = _STOP_REASONS.get(final.stop_reason or "", "other")
        detail = None
        if stop == "refusal" and final.stop_details:
            detail = final.stop_details.explanation or final.stop_details.category

        yield ChatResult(
            message=Message(
                role="assistant",
                content=text,
                tool_calls=tool_calls,
                provider_data={"provider": self.name, "model": final.model, "content": blocks},
            ),
            stop_reason=stop,
            detail=detail,
        )

    def _to_api_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "user":
                out.append({"role": "user", "content": m.content})
            elif m.role == "assistant":
                out.append({"role": "assistant", "content": self._assistant_content(m)})
            elif m.role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id,
                    "content": m.content,
                    "is_error": m.is_error,
                }
                # All results for one assistant turn go back in a single user message.
                prev = out[-1] if out else None
                if prev and prev["role"] == "user" and isinstance(prev["content"], list):
                    prev["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return out

    def _assistant_content(self, m: Message) -> Any:
        data = m.provider_data or {}
        if data.get("provider") == self.name and data.get("model") == self.model:
            # Replay our own turn unchanged so thinking blocks stay valid.
            return data["content"]
        content: list[dict[str, Any]] = []
        if m.content:
            content.append({"type": "text", "text": m.content})
        for c in m.tool_calls:
            content.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
        return content or [{"type": "text", "text": "(no content)"}]


def _after_fallback_boundary(blocks: list[Any]) -> list[Any]:
    """After a mid-output fallback, only text blocks before the last `fallback` block may be echoed back."""
    last = max((i for i, b in enumerate(blocks) if b.type == "fallback"), default=-1)
    if last < 0:
        return list(blocks)
    return [b for b in blocks[:last] if b.type == "text"] + list(blocks[last + 1 :])
