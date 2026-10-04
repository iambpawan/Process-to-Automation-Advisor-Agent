"""Cost and ROI: rules-only automation vs automation that also uses AI."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from advisor.models import ProcessProfile, Step

if TYPE_CHECKING:
    from advisor.integrations.licenses import LicenseInventory


@dataclass
class Scenario:
    name: str
    automated_steps: list[str]
    labor_saved_monthly: float
    pa_license_monthly: float
    ai_monthly: float
    support_monthly: float
    bots: int
    build_days: float
    build_cost: float
    net_monthly: float
    payback_months: float | None
    horizon_value: float
    roi_pct: float | None
    license_offset_monthly: float = 0.0  # licence/credit cost avoided by reusing spare licences
    offset_notes: list[str] = field(default_factory=list)

    @property
    def running_monthly(self) -> float:
        return self.pa_license_monthly + self.ai_monthly + self.support_monthly


@dataclass
class CostComparison:
    currency: str
    horizon_years: int
    current_monthly_cost: float
    current_hours_monthly: float
    ai_cost_per_transaction: float
    ai_monthly_cost: float
    ai_steps_labor_saved_monthly: float
    rules_only: Scenario
    with_ai: Scenario
    assumptions: list[str] = field(default_factory=list)


def is_ui_only(profile: ProcessProfile, step: Step) -> bool:
    system = profile.system_by_name(step.system)
    return bool(system and (system.is_legacy_desktop or system.has_api_or_connector is False))


def ai_cost_per_step(step: Step, pricing: dict[str, Any]) -> float:
    ai = pricing["ai"]
    if ai["engine"] == "ai_builder":
        return ai["ai_builder"]["credits_per_ai_step"] * ai["ai_builder"]["price_per_credit"]
    tokens = ai["tokens_per_step_by_input_type"][step.input_type or "unstructured"]
    price = ai["models_per_mtok"][ai["runtime_model"]]
    return (tokens["input"] * price["input"] + tokens["output"] * price["output"]) / 1_000_000


def _scenario(
    name: str,
    profile: ProcessProfile,
    automated: list[Step],
    ai_steps: list[Step],
    build_days: float,
    pricing: dict[str, Any],
    inventory: LicenseInventory | None = None,
) -> Scenario:
    volume = profile.transactions_per_month or 0
    rate = profile.hourly_labor_cost or 0
    pa = pricing["power_automate"]
    years = pricing["horizon_years"]

    saved_min = sum((s.minutes_per_run or 0) * (1 - (s.exception_rate_pct or 0) / 100) for s in automated)
    if ai_steps:
        saved_min -= pricing["labor"]["ai_review_minutes_per_transaction"]
    labor_saved = max(0.0, volume * saved_min / 60 * rate)

    ui_minutes = sum((s.minutes_per_run or 0) for s in automated if is_ui_only(profile, s))
    bots = math.ceil(volume * ui_minutes * pa["bot_runtime_ratio"] / pa["bot_capacity_minutes_per_month"]) if ui_minutes else 0
    licenses = (pa["premium_per_user_month"] * pa["flow_owner_licenses"] if automated else 0) + bots * pa[
        "process_license_per_bot_month"
    ]
    ai_monthly = volume * sum(ai_cost_per_step(s, pricing) for s in ai_steps)
    offset, offset_notes = 0.0, []
    if inventory is not None:
        license_offset, ai_share, offset_notes = _offset(inventory, bool(automated), bots, ai_steps, volume, pricing)
        ai_offset = ai_monthly * ai_share
        licenses -= license_offset
        ai_monthly -= ai_offset
        offset = license_offset + ai_offset

    build_cost = build_days * pricing["labor"]["build_day_rate"]
    support = build_cost * pricing["labor"]["support_pct_of_build_per_year"] / 12
    net = labor_saved - licenses - ai_monthly - support
    payback = build_cost / net if net > 0 else None
    value = net * 12 * years - build_cost
    roi = value / build_cost * 100 if build_cost else None
    return Scenario(
        name=name,
        automated_steps=[s.name for s in automated],
        labor_saved_monthly=labor_saved,
        pa_license_monthly=licenses,
        ai_monthly=ai_monthly,
        support_monthly=support,
        bots=bots,
        build_days=build_days,
        build_cost=build_cost,
        net_monthly=net,
        payback_months=payback,
        horizon_value=value,
        roi_pct=roi,
        license_offset_monthly=offset,
        offset_notes=offset_notes,
    )


def _offset(
    inventory: LicenseInventory,
    has_flow: bool,
    bots: int,
    ai_steps: list[Step],
    volume: float,
    pricing: dict[str, Any],
) -> tuple[float, float, list[str]]:
    """Licence cost covered by spare licences, and the share (0-1) of AI Builder credits covered."""
    pa = pricing["power_automate"]
    ai = pricing["ai"]
    notes: list[str] = []
    licenses = ai_share = 0.0
    owners = pa["flow_owner_licenses"] if has_flow else 0
    reused_owners = min(owners, max(0, inventory.premium_available))
    if reused_owners:
        licenses += reused_owners * pa["premium_per_user_month"]
        notes.append(f"{reused_owners} of {owners} Premium owner licence(s) from spare Premium seats")
    reused_bots = min(bots, max(0, inventory.process_available))
    if reused_bots:
        licenses += reused_bots * pa["process_license_per_bot_month"]
        notes.append(f"{reused_bots} of {bots} bot(s) from spare Process licences")
    if ai_steps and ai["engine"] == "ai_builder":
        needed = volume * len(ai_steps) * ai["ai_builder"]["credits_per_ai_step"]
        covered = min(needed, max(0, inventory.ai_builder_credits_available))
        if covered:
            ai_share = covered / needed
            notes.append(f"{covered:,.0f} of {needed:,.0f} AI Builder credits/month from spare capacity")
    return licenses, ai_share, notes


def compare(
    profile: ProcessProfile,
    ai_step_names: set[str],
    rules_build_days: float,
    ai_build_days: float,
    pricing: dict[str, Any],
    currency: str,
    inventory: LicenseInventory | None = None,
) -> CostComparison:
    """Compare rules-only and rules+AI scenarios.

    With `inventory`, spare licences already owned (Premium seats, Process licences, AI Builder
    credits) are not charged again; without it the full licence cost is counted.
    """
    volume = profile.transactions_per_month or 0
    rate = profile.hourly_labor_cost or 0
    # Approvals stay human in every scenario; the flow only routes them.
    automatable = [s for s in profile.steps if not s.is_approval]
    rule_steps = [s for s in automatable if s.name not in ai_step_names]
    ai_steps = [s for s in automatable if s.name in ai_step_names]

    rules_only = _scenario("Rules-only", profile, rule_steps, [], rules_build_days, pricing, inventory)
    with_ai = _scenario("Rules + AI", profile, automatable, ai_steps, ai_build_days, pricing, inventory)

    per_txn = sum(ai_cost_per_step(s, pricing) for s in ai_steps)
    ai_saved_min = sum((s.minutes_per_run or 0) * (1 - (s.exception_rate_pct or 0) / 100) for s in ai_steps)
    if ai_steps:
        ai_saved_min -= pricing["labor"]["ai_review_minutes_per_transaction"]
    hours = volume * profile.total_minutes() / 60

    ai = pricing["ai"]
    pa = pricing["power_automate"]
    assumptions = [
        f"Build day rate {pricing['labor']['build_day_rate']:,.0f} {currency}",
        f"Power Automate Premium {pa['premium_per_user_month']:,.2f} {currency}/user/month; "
        f"Process licence {pa['process_license_per_bot_month']:,.2f} {currency}/bot/month",
        f"AI review time {pricing['labor']['ai_review_minutes_per_transaction']} min per transaction",
        f"Support and maintenance {pricing['labor']['support_pct_of_build_per_year']:.0%} of build cost per year",
        "Approval steps stay with people in every scenario",
        f"AI engine: {ai['engine']}" + (f" ({ai['runtime_model']})" if ai["engine"] == "llm_tokens" else ""),
        "Exceptions stay manual; savings exclude the exception share of each step",
        pricing.get("_note", ""),
    ]
    if inventory is not None:
        assumptions.insert(-1, _offset_assumption(inventory, rules_only, with_ai, ai, currency))
    return CostComparison(
        currency=currency,
        horizon_years=pricing["horizon_years"],
        current_monthly_cost=hours * rate,
        current_hours_monthly=hours,
        ai_cost_per_transaction=per_txn,
        ai_monthly_cost=per_txn * volume,
        ai_steps_labor_saved_monthly=max(0.0, volume * ai_saved_min / 60 * rate),
        rules_only=rules_only,
        with_ai=with_ai,
        assumptions=[a for a in assumptions if a],
    )


def _offset_assumption(
    inventory: LicenseInventory, rules_only: Scenario, with_ai: Scenario, ai: dict[str, Any], currency: str
) -> str:
    spare = (
        f"Spare licences in tenant: {inventory.premium_available} Premium, {inventory.process_available} Process, "
        f"{inventory.ai_builder_credits_available:,} AI Builder credits/month"
    )
    parts = []
    for sc in (rules_only, with_ai):
        if sc.offset_notes:
            parts.append(f"{sc.name} reuses " + ", ".join(sc.offset_notes)
                         + f" (saves {sc.license_offset_monthly:,.2f} {currency}/month)")
        else:
            parts.append(f"{sc.name}: no spare licences apply")
    text = f"{spare}. " + "; ".join(parts)
    if inventory.ai_builder_credits_available and ai["engine"] != "ai_builder":
        text += "; AI Builder credits not used because the AI engine is LLM tokens"
    return text
