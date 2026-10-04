"""Provider-neutral agent loop: model turn -> run requested tools -> repeat."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator

from pydantic import ValidationError

from advisor.llm.base import ChatResult, LLMProvider, Message, ProviderError, TextDelta, ToolInputError, ToolSpec

MAX_TOOL_ROUNDS = 12
MAX_RETRIES = 2


@dataclass
class ToolStarted:
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolFinished:
    name: str
    result: str
    is_error: bool


@dataclass
class Notice:
    text: str


AgentEvent = TextDelta | ToolStarted | ToolFinished | Notice


class LLMAgent:
    def __init__(self, provider: LLMProvider, system: str, tools: list[ToolSpec] | None = None):
        self.provider = provider
        self.system = system
        self.tools = tools or []
        self._by_name = {t.name: t for t in self.tools}

    def run_turn(self, history: list[Message], user_text: str) -> Iterator[AgentEvent]:
        """Append the user's message, then loop until the model stops calling tools.

        `history` is only ever appended to, so providers can cache and replay it.
        """
        history.append(Message(role="user", content=user_text))
        for _ in range(MAX_TOOL_ROUNDS):
            result = yield from self._model_turn(history)
            history.append(result.message)

            if result.stop_reason == "refusal":
                yield Notice("The model declined this request" + (f": {result.detail}" if result.detail else "."))
                return
            if result.stop_reason == "max_tokens":
                yield Notice("The response was cut off at the output limit.")
                if result.message.tool_calls:
                    # A truncated tool call must not run; answer it so history stays valid.
                    for call in result.message.tool_calls:
                        history.append(
                            Message(role="tool", tool_call_id=call.id, content="Not run: output truncated.", is_error=True)
                        )
                return
            if not result.message.tool_calls:
                return
            for call in result.message.tool_calls:
                yield ToolStarted(call.name, call.arguments)
                output, is_error = self._run_tool(call.name, call.arguments)
                yield ToolFinished(call.name, output, is_error)
                history.append(Message(role="tool", tool_call_id=call.id, content=output, is_error=is_error))
        yield Notice("Stopped after too many tool rounds.")

    def _model_turn(self, history: list[Message]) -> Iterator[AgentEvent]:
        for attempt in range(MAX_RETRIES + 1):
            try:
                for event in self.provider.chat(history, self.tools, self.system):
                    if isinstance(event, ChatResult):
                        return event
                    yield event
                raise ProviderError("Provider ended the turn without a result.")
            except ProviderError as e:
                if not e.retryable or attempt == MAX_RETRIES:
                    raise
                yield Notice(f"{e} Retrying…")
        raise AssertionError("unreachable")

    def _run_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        tool = self._by_name.get(name)
        if tool is None:
            return f"Unknown tool '{name}'.", True
        try:
            args = tool.input_model.model_validate(arguments)
            output = tool.handler(args)
        except ValidationError as e:
            return f"Invalid arguments: {e.errors(include_url=False)}", True
        except ToolInputError as e:
            return str(e), True
        return (output if isinstance(output, str) else json.dumps(output, default=str)), False
