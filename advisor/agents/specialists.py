"""Deterministic specialist agents. Each wraps plain-Python analysis — no LLM calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from advisor.analysis import ai_need, compliance, cost, delivery, fit, pricing_in
from advisor.integrations import FlowInfo, LicenseInventory, Overlap, find_overlaps
from advisor.models import ProcessProfile


@dataclass
class Assessment:
    profile: ProcessProfile
    currency: str
    compliance: compliance.ComplianceResult | None = None
    signals: list[ai_need.StepSignal] = field(default_factory=list)
    rules_effort: delivery.Effort | None = None
    ai_effort: delivery.Effort | None = None
    costs: cost.CostComparison | None = None
    ai_decision: ai_need.AINeedResult | None = None
    chosen: cost.Scenario | None = None
    effort: delivery.Effort | None = None
    fit: fit.FitScore | None = None
    step_plans: list[delivery.StepPlan] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    maintenance: delivery.Maintenance | None = None
    # Tenant inventory (optional uploads): existing flows, their overlaps with steps, spare licences.
    flows: list[FlowInfo] = field(default_factory=list)
    overlaps: list[Overlap] = field(default_factory=list)
    inventory: LicenseInventory | None = None


class ComplianceAgent:
    def __init__(self, llm_data_region: str):
        self.llm_data_region = llm_data_region

    def run(self, a: Assessment) -> None:
        a.compliance = compliance.assess(a.profile, self.llm_data_region)


class AINeedAgent:
    def detect(self, a: Assessment) -> None:
        a.signals = ai_need.detect_signals(a.profile)

    def decide(self, a: Assessment) -> None:
        a.ai_decision = ai_need.decide(a.profile, a.signals, a.costs, a.compliance)


class CostAgent:
    def __init__(self, pricing: dict[str, Any]):
        self.pricing = pricing

    def run(self, a: Assessment) -> None:
        pricing = pricing_in(self.pricing, a.currency)
        ai_steps = {s.step for s in a.signals}
        sensitive = a.compliance.sensitive
        a.rules_effort = delivery.estimate_effort(a.profile, set(), sensitive, pricing)
        a.ai_effort = delivery.estimate_effort(a.profile, ai_steps, sensitive, pricing)
        a.costs = cost.compare(a.profile, ai_steps, a.rules_effort.days, a.ai_effort.days, pricing, a.currency,
                               inventory=a.inventory)


class AnalystAgent:
    def run(self, a: Assessment) -> None:
        ai_used = a.ai_decision.ai_needed
        a.chosen = a.costs.with_ai if ai_used else a.costs.rules_only
        a.effort = a.ai_effort if ai_used else a.rules_effort
        a.fit = fit.score(a.profile, a.chosen)
        ai_steps = {s.step for s in a.signals}
        a.step_plans = delivery.plan_steps(a.profile, ai_steps, ai_used, a.compliance.sensitive)
        a.platforms = delivery.platform_summary(a.step_plans, ai_used, a.profile)
        a.maintenance = delivery.maintenance_risk(a.profile, ai_used)


class ReuseAgent:
    """Flags steps that existing Power Automate flows may already cover (word matching, no LLM)."""

    def run(self, a: Assessment) -> None:
        a.overlaps = find_overlaps(a.profile, a.flows) if a.flows else []
