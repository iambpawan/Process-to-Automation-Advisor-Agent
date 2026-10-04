"""Stage 3: does this process need AI, and is AI worth it?"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from advisor.analysis.compliance import ComplianceResult
from advisor.analysis.cost import CostComparison
from advisor.models import AI_INPUT_TYPES, ProcessProfile

Approach = Literal["Rules-only", "Hybrid", "AI-first"]

HYBRID_MAX_AI_SHARE = 0.4

BENEFITS = [
    "Handles variation in wording and layout that rules cannot anticipate",
    "Reads unstructured inputs (emails, free text, scanned documents)",
    "Reduces exceptions that would otherwise fall back to staff",
]
LIMITATIONS = [
    "Can produce confident but wrong output (hallucination) — needs validation",
    "Costs money on every call, so cost grows with volume",
    "Output is less predictable than fixed rules; needs monitoring for drift",
    "Needs a human review step for decisions that matter",
    "Raises data-privacy questions about what is sent to the model",
]


@dataclass
class StepSignal:
    step: str
    reasons: list[str]


@dataclass
class AINeedResult:
    approach: Approach
    ai_needed: bool
    reason: str
    ai_steps: list[StepSignal]
    ai_minutes_share: float
    benefits: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    alternative: str | None = None


def detect_signals(profile: ProcessProfile) -> list[StepSignal]:
    signals = []
    for s in profile.steps:
        if s.is_approval:
            continue  # sign-offs stay with a person by design
        reasons = []
        if s.input_type in AI_INPUT_TYPES:
            reasons.append(f"{s.input_type.replace('_', ' ')} input")
        if s.requires_judgment:
            reasons.append("judgment call")
        if s.needs_language_understanding:
            reasons.append("language understanding")
        if reasons:
            signals.append(StepSignal(s.name, reasons))
    return signals


def decide(
    profile: ProcessProfile,
    signals: list[StepSignal],
    costs: CostComparison,
    compliance: ComplianceResult,
) -> AINeedResult:
    total = profile.total_minutes()
    ai_names = {sig.step for sig in signals}
    ai_minutes = sum(s.minutes_per_run or 0 for s in profile.steps if s.name in ai_names)
    share = ai_minutes / total if total else 0.0

    def result(approach: Approach, reason: str, alternative: str | None = None) -> AINeedResult:
        uses_ai = approach != "Rules-only"
        limitations = list(LIMITATIONS)
        if compliance.sensitive:
            limitations[-1] += " — critical here because the data includes " + ", ".join(compliance.categories)
        return AINeedResult(
            approach=approach,
            ai_needed=uses_ai,
            reason=reason,
            ai_steps=signals,
            ai_minutes_share=share,
            benefits=BENEFITS if (uses_ai or signals) else [],
            limitations=limitations if (uses_ai or signals) else [],
            alternative=alternative,
        )

    if not signals:
        return result(
            "Rules-only",
            "AI not needed: every step is rule-based with structured inputs, so plain Power Automate "
            "(cloud or desktop flows) is enough.",
        )

    step_list = ", ".join(sorted(ai_names))
    if not compliance.can_send_to_llm:
        return result(
            "Rules-only",
            f"AI would help with {step_list}, but the data cannot be sent to the configured LLM. "
            "Automate the rule-based steps and keep those steps manual for now.",
            alternative="Revisit as Hybrid with a UAE-hosted model or de-identified inputs.",
        )

    rules, ai = costs.rules_only, costs.with_ai
    if ai.horizon_value < rules.horizon_value:
        return result(
            "Rules-only",
            f"AI could automate {step_list}, but over {costs.horizon_years} years it returns less than rules-only "
            f"automation ({ai.horizon_value:,.0f} vs {rules.horizon_value:,.0f} {costs.currency}): AI cost "
            f"{costs.ai_monthly_cost:,.0f}/month against {costs.ai_steps_labor_saved_monthly:,.0f}/month of labour saved "
            "on those steps.",
            alternative="Hybrid becomes worthwhile if volume grows or a cheaper model is used.",
        )

    if share <= HYBRID_MAX_AI_SHARE:
        return result(
            "Hybrid",
            f"Rules handle most of the work; AI is needed only for {step_list} "
            f"({share:.0%} of handling time) and pays back within the horizon.",
        )
    return result(
        "AI-first",
        f"{share:.0%} of handling time sits in steps that need AI ({step_list}) — above the "
        f"{HYBRID_MAX_AI_SHARE:.0%} hybrid threshold — so AI is central to the design; rules alone would leave "
        "much of the work manual.",
    )
