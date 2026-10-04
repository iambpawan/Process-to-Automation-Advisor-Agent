"""Writer Agent: turns the assessment into a solution design document."""

from __future__ import annotations

import json
from typing import Iterator

from advisor.agents.base import AgentEvent, LLMAgent
from advisor.agents.specialists import Assessment
from advisor.llm.base import LLMProvider, Message

SYSTEM_PROMPT = """You are the Writer in a process-automation advisory team. You write the solution design \
document for a business and IT audience, in Markdown, from facts the analysts give you.

The analysts computed every score, cost, saving, payback, effort and percentage in code. Those figures are \
inserted into your document through placeholder tokens, so you must not write any numbers of your own — no \
scores, money, hours, minutes, days, percentages or counts. Refer to them in words ("see the cost comparison \
below") and let the placeholders carry the figures. Do not change the recommendation or invent facts, \
systems, tools or regulations that are not in the input.

Use exactly this outline, and put each placeholder token on its own line where shown:

# Solution Design: <process name>
## 1. Executive summary
(short paragraph: the process, the recommendation, and why)
## 2. Process summary
(short narrative of how the process works today)
{{PROCESS_TABLE}}
## 3. Automation fit
{{FIT_SCORE}}
(two or three sentences interpreting the strongest and weakest factors)
## 4. AI decision
{{AI_DECISION}}
(short narrative on where AI does and doesn't belong in this process)
## 5. Target architecture
(describe the end-to-end automated flow step by step, using the recommended tools and human review points)
{{DELIVERY}}
(one or two sentences on reusing the existing flows and spare licences listed in the facts; if none were provided, say the tenant inventory was not reviewed)
{{EXISTING_AUTOMATION}}
## 6. Risks and compliance
{{COMPLIANCE}}
(bulleted risks with a mitigation for each, covering data privacy, maintenance and AI risks where relevant)
## 7. Cost and ROI
{{COST_TABLE}}
## 8. Next steps
(numbered, practical next steps, starting with validating the pricing assumptions)"""


def _facts(a: Assessment) -> dict:
    p = a.profile
    return {
        "process": {"name": p.name, "description": p.description, "department": p.department, "industry": p.industry,
                    "frequency": p.frequency, "stability": p.process_stability},
        "steps": [{"name": s.name, "description": s.description, "system": s.system,
                   "input_type": s.input_type, "requires_judgment": s.requires_judgment,
                   "is_approval": s.is_approval} for s in p.steps],
        "systems": [s.model_dump() for s in p.systems],
        "fit_band": a.fit.band,
        "recommended_approach": a.ai_decision.approach,
        "ai_needed": a.ai_decision.ai_needed,
        "ai_signal_steps": [{"step": s.step, "signals": s.reasons} for s in a.ai_decision.ai_steps],
        "alternative": a.ai_decision.alternative,
        "step_plan": [{"step": sp.step, "tool": sp.tool, "human_review": sp.human_reason} for sp in a.step_plans],
        "platform_components": a.platforms,
        "effort_size": a.effort.size,
        "maintenance_risk": {"level": a.maintenance.risk, "drivers": a.maintenance.drivers},
        "existing_flows_reviewed": [f.name for f in a.flows],
        "existing_flow_overlaps": [{"step": o.step, "existing_flow": o.flow} for o in a.overlaps],
        "licence_inventory_provided": a.inventory is not None,
        "compliance": {"sensitive_categories": a.compliance.categories, "can_send_to_llm": a.compliance.can_send_to_llm,
                       "llm_condition": a.compliance.llm_condition, "regulations": a.compliance.regulations,
                       "required_controls": a.compliance.required_controls},
    }


class WriterAgent(LLMAgent):
    def __init__(self, provider: LLMProvider):
        super().__init__(provider, SYSTEM_PROMPT)

    def write(self, a: Assessment, history: list[Message]) -> Iterator[AgentEvent]:
        prompt = "Write the solution design document from these facts:\n\n" + json.dumps(_facts(a), indent=2)
        yield from self.run_turn(history, prompt)
