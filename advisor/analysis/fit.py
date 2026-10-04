"""Stage 2: automation fit score (0-100)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from advisor.analysis.cost import Scenario
from advisor.models import ProcessProfile

INPUT_QUALITY = {
    "structured": 1.0,
    "semi_structured": 0.7,
    "email": 0.5,
    "unstructured": 0.4,
    "scanned_document": 0.3,
    "voice": 0.3,
}
STABILITY = {"stable": 1.0, "changes_quarterly": 0.6, "changes_monthly": 0.2}
FULL_VOLUME_HOURS = 160  # one FTE-month of manual effort earns full volume points


@dataclass
class Component:
    name: str
    points: float
    max_points: int
    note: str


@dataclass
class FitScore:
    total: int
    band: str
    components: list[Component]


def _weighted(profile: ProcessProfile, value) -> float:
    steps = profile.steps
    if not steps:
        return 0.0
    total = profile.total_minutes()
    if total == 0:
        return sum(value(s) for s in steps) / len(steps)
    return sum(value(s) * (s.minutes_per_run or 0) for s in steps) / total


def _rule_factor(step) -> float:
    if step.requires_judgment:
        return 0.2
    return 1.0 if step.rules_documented else 0.6


def _data_factor(step) -> float:
    quality = INPUT_QUALITY.get(step.input_type or "unstructured", 0.4)
    exc = min(step.exception_rate_pct or 0, 60)
    return quality * (1 - exc / 60 * 0.8)


def _roi_factor(payback: float | None) -> tuple[float, str]:
    if payback is None:
        return 0.0, "does not pay back"
    if payback <= 6:
        return 1.0, f"payback {payback:.1f} months"
    if payback <= 12:
        return 0.75, f"payback {payback:.1f} months"
    if payback <= 24:
        return 0.4, f"payback {payback:.1f} months"
    return 0.15, f"payback {payback:.1f} months"


def band(total: int) -> str:
    if total >= 75:
        return "Strong candidate — automate"
    if total >= 50:
        return "Good candidate — automate with human-in-the-loop"
    if total >= 30:
        return "Standardise the process first"
    return "Not recommended for automation now"


def score(profile: ProcessProfile, chosen: Scenario) -> FitScore:
    hours = (profile.transactions_per_month or 0) * profile.total_minutes() / 60
    volume = min(1.0, math.log10(1 + hours) / math.log10(1 + FULL_VOLUME_HOURS))
    stability = STABILITY.get(profile.process_stability or "changes_quarterly", 0.6)
    roi, roi_note = _roi_factor(chosen.payback_months)

    components = [
        Component("Rules clarity", 25 * _weighted(profile, _rule_factor), 25, "documented rules vs judgment, by handling time"),
        Component("Volume", 20 * volume, 20, f"{hours:,.0f} manual hours/month"),
        Component("Stability", 15 * stability, 15, (profile.process_stability or "unknown").replace("_", " ")),
        Component("Data quality", 20 * _weighted(profile, _data_factor), 20, "input structure and exception rates"),
        Component("ROI", 20 * roi, 20, f"{chosen.name}: {roi_note}"),
    ]
    for c in components:
        c.points = round(c.points, 1)
    total = round(sum(c.points for c in components))
    return FitScore(total=total, band=band(total), components=components)
