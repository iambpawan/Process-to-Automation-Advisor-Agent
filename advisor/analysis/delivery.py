"""Stage 4 checks: human-in-the-loop, tool recommendation, effort, maintenance risk."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from advisor.analysis.cost import is_ui_only
from advisor.models import ProcessProfile, Step

Size = Literal["Small", "Medium", "Large"]
Risk = Literal["Low", "Medium", "High"]

HIGH_EXCEPTION_PCT = 20


@dataclass
class Effort:
    days: float
    size: Size
    low_days: float
    high_days: float


@dataclass
class StepPlan:
    step: str
    tool: str
    needs_human: bool
    human_reason: str | None


@dataclass
class Maintenance:
    risk: Risk
    points: int
    drivers: list[str]


def estimate_effort(
    profile: ProcessProfile, ai_steps: set[str], sensitive: bool, pricing: dict[str, Any]
) -> Effort:
    e = pricing["effort_days"]
    used_systems = {s.system.lower() for s in profile.steps if s.system}
    days = e["base"] + e["per_step"] * len(profile.steps) + e["per_ai_step"] * len(ai_steps)
    for system in profile.systems:
        if system.name.lower() in used_systems:
            ui = system.is_legacy_desktop or system.has_api_or_connector is False
            days += e["per_ui_system"] if ui else e["per_api_system"]
    days += e["per_hitl_step"] * sum(1 for s in profile.steps if _human_reason(s, s.name in ai_steps, sensitive))
    if sensitive:
        days += e["compliance"]
    size: Size = "Small" if days <= e["small_max"] else "Medium" if days <= e["medium_max"] else "Large"
    return Effort(days=round(days, 1), size=size, low_days=round(days * 0.7, 1), high_days=round(days * 1.4, 1))


def _human_reason(step: Step, uses_ai: bool, sensitive: bool) -> str | None:
    if step.is_approval:
        return "Approval / sign-off"
    if step.requires_judgment and not uses_ai:
        return "Judgment call stays with staff"
    if uses_ai and sensitive:
        return "AI output on sensitive data must be reviewed"
    if uses_ai and step.requires_judgment:
        return "AI proposes, a person decides"
    if (step.exception_rate_pct or 0) >= HIGH_EXCEPTION_PCT:
        return f"High exception rate ({step.exception_rate_pct:.0f}%) — route exceptions to a person"
    return None


def _tool_for(profile: ProcessProfile, step: Step, uses_ai: bool, ai_signal: bool) -> str:
    if ai_signal and not uses_ai:
        return "Manual task (assigned through the flow)"
    if uses_ai:
        if step.input_type == "scanned_document":
            return "AI Builder document processing in a cloud flow"
        if step.input_type == "voice":
            return "Azure AI Speech + Azure AI Foundry"
        if step.requires_judgment:
            return "Azure AI Foundry (LLM with human review)"
        return "AI prompt (AI Builder / Azure OpenAI) in a cloud flow"
    if step.is_approval:
        return "Power Automate cloud flow + Approvals"
    if is_ui_only(profile, step):
        return "Power Automate desktop flow (unattended)"
    return "Power Automate cloud flow"


def plan_steps(
    profile: ProcessProfile, ai_signal_steps: set[str], ai_used: bool, sensitive: bool
) -> list[StepPlan]:
    plans = []
    for s in profile.steps:
        signal = s.name in ai_signal_steps
        uses_ai = ai_used and signal
        reason = _human_reason(s, uses_ai, sensitive)
        plans.append(StepPlan(s.name, _tool_for(profile, s, uses_ai, signal), reason is not None, reason))
    return plans


def platform_summary(plans: list[StepPlan], ai_used: bool, profile: ProcessProfile) -> list[str]:
    tools = sorted({p.tool for p in plans})
    if ai_used and any(s.input_type == "voice" for s in profile.steps):
        tools.append("Optional: Copilot Studio agent as a conversational front door for requesters")
    return tools


def maintenance_risk(profile: ProcessProfile, ai_used: bool) -> Maintenance:
    points, drivers = 0, []
    weights = {"frequent": 3, "occasional": 2, "rare": 1}
    used = {s.system.lower() for s in profile.steps if s.system}
    for system in profile.systems:
        if system.name.lower() in used and (system.is_legacy_desktop or system.has_api_or_connector is False):
            w = weights.get(system.ui_change_frequency or "occasional", 2)
            points += w
            how = {"rare": "rarely", "occasional": "occasionally", "frequent": "frequently"}.get(
                system.ui_change_frequency or "", "at an unknown rate")
            label = "A system still to be confirmed" if system.name == "Unknown system" else system.name
            drivers.append(f"{label}: UI automation, screens change {how}")
    if profile.process_stability == "changes_monthly":
        points += 3
        drivers.append("Process rules change monthly")
    elif profile.process_stability == "changes_quarterly":
        points += 1
        drivers.append("Process rules change quarterly")
    if ai_used:
        points += 1
        drivers.append("AI prompts/models need periodic re-validation")
    risk: Risk = "Low" if points <= 2 else "Medium" if points <= 5 else "High"
    return Maintenance(risk, points, drivers or ["API-based integrations on a stable process"])
