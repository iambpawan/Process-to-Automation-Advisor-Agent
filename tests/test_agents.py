"""Agent loop + interviewer + writer, driven by a scripted fake provider (no API calls)."""

from pathlib import Path
from typing import Iterator

from advisor.agents.base import Notice, ToolFinished
from advisor.agents.interviewer import InterviewerAgent
from advisor.config import Settings
from advisor.llm.base import ChatEvent, ChatResult, LLMProvider, Message, TextDelta, ToolCall, ToolSpec
from advisor.models import ProcessProfile
from advisor.orchestrator import Orchestrator
from advisor.report import PLACEHOLDERS, to_docx

SAMPLE = Path(__file__).resolve().parent.parent / "advisor" / "samples" / "claims_intake.json"
SETTINGS = Settings("claude", "fake", None, "high", "US", "", "USD")


class FakeProvider(LLMProvider):
    name = "fake"
    model = "fake"

    def __init__(self, turns: list[ChatResult]):
        self.turns = list(turns)
        self.seen: list[list[Message]] = []

    def chat(self, messages, tools=None, system=None) -> Iterator[ChatEvent]:
        self.seen.append(list(messages))
        result = self.turns.pop(0)
        if result.message.content:
            yield TextDelta(result.message.content)
        yield result


def reply(text="", calls=(), stop="end_turn") -> ChatResult:
    return ChatResult(Message(role="assistant", content=text, tool_calls=list(calls)), stop)


def test_interviewer_records_facts_and_returns_missing():
    profile = ProcessProfile()
    call = ToolCall("t1", "update_profile", {"name": "Invoice matching", "transactions_per_month": 500})
    provider = FakeProvider([reply(calls=[call], stop="tool_use"), reply("How many steps are there?")])
    agent = InterviewerAgent(provider, profile)
    history: list[Message] = []

    events = list(agent.run_turn(history, "We match invoices, about 500 a month."))

    assert profile.name == "Invoice matching"
    assert profile.transactions_per_month == 500
    finished = [e for e in events if isinstance(e, ToolFinished)]
    assert finished and not finished[0].is_error and "still_missing" in finished[0].result
    assert [m.role for m in history] == ["user", "assistant", "tool", "assistant"]
    assert history[2].tool_call_id == "t1"


def test_invalid_tool_args_return_error_to_model():
    profile = ProcessProfile()
    bad = ToolCall("t1", "update_profile", {"transactions_per_month": -5})
    provider = FakeProvider([reply(calls=[bad], stop="tool_use"), reply("Could you confirm the volume?")])
    events = list(InterviewerAgent(provider, profile).run_turn([], "hi"))
    finished = [e for e in events if isinstance(e, ToolFinished)][0]
    assert finished.is_error
    assert profile.transactions_per_month is None


def test_finish_rejected_while_fields_missing():
    profile = ProcessProfile()
    call = ToolCall("t1", "finish_interview", {"summary": "x"})
    agent = InterviewerAgent(FakeProvider([reply(calls=[call], stop="tool_use"), reply("ok")]), profile)
    events = list(agent.run_turn([], "done"))
    assert [e for e in events if isinstance(e, ToolFinished)][0].is_error
    assert not agent.complete


def test_refusal_stops_loop():
    agent = InterviewerAgent(FakeProvider([reply(stop="refusal")]), ProcessProfile())
    events = list(agent.run_turn([], "hi"))
    assert any(isinstance(e, Notice) for e in events)


def test_tool_schema_is_flat():
    spec = InterviewerAgent(FakeProvider([]), ProcessProfile()).tools[0]
    assert isinstance(spec, ToolSpec)
    assert "$ref" not in str(spec.parameters) and "$defs" not in spec.parameters


def test_writer_numbers_come_from_python():
    profile = ProcessProfile.model_validate_json(SAMPLE.read_text(encoding="utf-8"))
    orch = Orchestrator(SETTINGS)
    a = orch.analyze(profile)
    narrative = "# Solution Design: Claims\n## 3. Automation fit\n{{FIT_SCORE}}\n## 7. Cost and ROI\n{{COST_TABLE}}"
    out = list(orch.write_document(a, FakeProvider([reply(narrative)])))
    doc = out[-1]
    assert isinstance(doc, str)
    assert "{{" not in doc
    assert f"{a.fit.total} / 100" in doc
    assert "Appendix" in doc  # placeholders the writer skipped are appended
    assert to_docx(doc)[:2] == b"PK"  # valid .docx (zip)
    assert len(PLACEHOLDERS) == 7
