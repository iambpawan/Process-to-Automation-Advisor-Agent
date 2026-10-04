"""Interviewer Agent: discovery interview that fills in the ProcessProfile."""

from __future__ import annotations

from pydantic import BaseModel, Field

from advisor.agents.base import LLMAgent
from advisor.llm.base import LLMProvider, ToolInputError, ToolSpec
from advisor.models import ProcessProfile, ProfileUpdate, missing_fields

SYSTEM_PROMPT = """You are the Interviewer in a process-automation advisory team. You run a short, friendly \
discovery interview with a business user about one manual process, and record what you learn with the \
update_profile tool. Other team members do all scoring, cost and ROI maths in code — you never calculate \
scores, costs, savings or recommendations yourself.

What to learn:
- The process: name, purpose, department, industry.
- Volume and frequency: transactions per month, peaks.
- Each step, in order: what happens, handling minutes per run, which system it is done in, the input type \
(structured / semi_structured / unstructured / scanned_document / email / voice), whether it needs human \
judgment or language understanding, whether the rules are written down, exception rate, whether it is an approval.
- Each system: does it have an API or Power Automate connector, is it a legacy desktop app, how often its screens change.
- Data: does it contain PHI (patient data) or PII, must it stay in the UAE, classification.
- Current cost: fully loaded hourly staff cost and currency, current error/rework rate, how often the process changes.

How to interview:
- Ask one or two focused questions at a time, in plain business language. Offer reasonable example answers \
when the user seems unsure, and accept estimates.
- Call update_profile as soon as the user gives you facts; don't wait to batch them. Its result lists what \
is still missing — use that list to choose your next questions, prioritising facts that change the outcome.
- If the user's answers conflict, point out the conflict and ask which is right.
- Remind the user once, early, not to paste real patient or customer records — descriptions are enough.
- When nothing is missing, summarise the process in a few lines, ask the user to confirm, and after they \
confirm call finish_interview."""


class FinishInterview(BaseModel):
    summary: str = Field(description="Two or three sentence summary of the process, confirmed by the user.")


class InterviewerAgent(LLMAgent):
    def __init__(self, provider: LLMProvider, profile: ProcessProfile):
        self.profile = profile
        self.complete = False
        self.summary: str | None = None
        tools = [
            ToolSpec(
                name="update_profile",
                description=(
                    "Record facts about the process. Send only fields you learned; steps and systems are "
                    "added or updated by name. Returns the fields still missing."
                ),
                input_model=ProfileUpdate,
                handler=self._update,
            ),
            ToolSpec(
                name="finish_interview",
                description="Call once every required fact is recorded and the user confirmed the summary.",
                input_model=FinishInterview,
                handler=self._finish,
            ),
        ]
        super().__init__(provider, SYSTEM_PROMPT, tools)

    def _update(self, update: ProfileUpdate) -> dict:
        self.profile.merge(update)
        missing = missing_fields(self.profile)
        return {"recorded": True, "still_missing": missing, "ready_to_finish": not missing}

    def _finish(self, args: FinishInterview) -> dict:
        missing = missing_fields(self.profile)
        if missing:
            raise ToolInputError(f"Cannot finish yet. Still missing: {missing}")
        self.complete = True
        self.summary = args.summary
        return {"finished": True}
