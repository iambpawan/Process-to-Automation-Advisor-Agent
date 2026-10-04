"""Orchestrator: runs the specialist agents in order and hands the result to the Writer."""

from __future__ import annotations

from typing import Any, Iterable, Iterator

from advisor.agents.base import AgentEvent
from advisor.agents.specialists import (
    AINeedAgent,
    AnalystAgent,
    Assessment,
    ComplianceAgent,
    CostAgent,
    ReuseAgent,
)
from advisor.agents.template_writer import TemplateWriter
from advisor.agents.writer import WriterAgent
from advisor.analysis import load_pricing
from advisor.config import Settings
from advisor.integrations import FlowInfo, LicenseInventory
from advisor.llm.base import LLMProvider, Message
from advisor.models import ProcessProfile, missing_fields, normalize_currency
from advisor.report import fill

UAE_AZURE_REGIONS = ("uaenorth", "uaecentral")


def runtime_data_region(runtime_model: str | None, default: str) -> str:
    """Where the AI that runs inside the automation processes data.

    Live Azure OpenAI entries carry their deployment in the key: "(uaenorth)" keeps data in
    the UAE, "(global)" may process it in any Azure region. Other models use LLM_DATA_REGION.
    """
    name = (runtime_model or "").lower()
    if not name.startswith("azure-openai-") or not name.endswith(")"):
        return default
    deployment = name.rsplit("(", 1)[-1].rstrip(")")
    if deployment in UAE_AZURE_REGIONS:
        return "UAE"
    if deployment == "global":
        return "GLOBAL (any Azure region)"
    return f"Azure {deployment}"


class Orchestrator:
    def __init__(self, settings: Settings, pricing: dict[str, Any] | None = None):
        self.settings = settings
        self.pricing = pricing or load_pricing()
        runtime = self.pricing.get("ai", {}).get("runtime_model")
        self.data_region = runtime_data_region(runtime, settings.llm_data_region)
        self.compliance = ComplianceAgent(self.data_region)
        self.ai_need = AINeedAgent()
        self.cost = CostAgent(self.pricing)
        self.analyst = AnalystAgent()
        self.reuse = ReuseAgent()

    def analyze(
        self,
        profile: ProcessProfile,
        flows: Iterable[FlowInfo] | None = None,
        inventory: LicenseInventory | None = None,
    ) -> Assessment:
        missing = missing_fields(profile)
        if missing:
            raise ValueError(f"Profile incomplete: {missing}")
        a = Assessment(
            profile=profile.model_copy(deep=True),  # a finished assessment must not change if the live profile is edited later
            currency=normalize_currency(profile.currency) or self.settings.currency,
            flows=list(flows or []),
            inventory=inventory,
        )
        self.compliance.run(a)      # PHI/PII/UAE first: it can veto AI
        self.ai_need.detect(a)      # which steps show AI signals
        self.cost.run(a)            # effort + rules-only vs rules+AI costs (spare licences offset)
        self.ai_need.decide(a)      # Rules-only / Hybrid / AI-first
        self.analyst.run(a)         # fit score, tools, HITL, maintenance
        self.reuse.run(a)           # existing flows that may already cover steps
        return a

    def write_document(self, a: Assessment, provider: LLMProvider | None = None) -> Iterator[AgentEvent | str]:
        """Streams Writer events, then yields the final filled-in Markdown document (a str).

        With no provider (offline mode) the deterministic TemplateWriter writes the narrative.
        """
        history: list[Message] = []
        writer = WriterAgent(provider) if provider is not None else TemplateWriter()
        yield from writer.write(a, history)
        narrative = next((m.content for m in reversed(history) if m.role == "assistant" and m.content), "")
        yield fill(narrative, a)
