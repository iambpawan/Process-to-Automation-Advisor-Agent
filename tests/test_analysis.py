import dataclasses
from pathlib import Path

import pytest

from advisor.analysis import load_pricing, pricing_in
from advisor.analysis.cost import ai_cost_per_step
from advisor.config import Settings
from advisor.models import ProcessProfile, ProfileUpdate, Step, missing_fields
from advisor.orchestrator import Orchestrator

SAMPLE = Path(__file__).resolve().parent.parent / "advisor" / "samples" / "claims_intake.json"


def settings(region: str = "US") -> Settings:
    return Settings("claude", "claude-opus-5-5", None, "high", region, "http://localhost:11434", "USD")


@pytest.fixture
def profile() -> ProcessProfile:
    return ProcessProfile.model_validate_json(SAMPLE.read_text(encoding="utf-8"))


def test_sample_is_complete(profile):
    assert missing_fields(profile) == []


def test_missing_fields_on_empty_profile():
    missing = missing_fields(ProcessProfile())
    assert "transactions_per_month" in missing
    assert "steps (at least 2)" in missing


def test_merge_upserts_steps_by_name(profile):
    profile.merge(ProfileUpdate(steps=[Step(name="register claim", minutes_per_run=9)]))
    step = next(s for s in profile.steps if s.name == "Register claim")
    assert step.minutes_per_run == 9
    assert step.system == "Legacy Claims System"  # untouched fields survive
    assert len(profile.steps) == 6


def test_phi_with_uae_residency_blocks_us_llm(profile):
    a = Orchestrator(settings("US")).analyze(profile)
    assert not a.compliance.can_send_to_llm
    assert a.ai_decision.approach == "Rules-only"
    assert a.ai_decision.alternative
    # The approval step needs judgment but stays human, so it is not an AI candidate.
    assert {s.step for s in a.signals} == {"Triage claim email", "Extract claim form data"}


def test_uae_hosted_llm_allows_ai(profile):
    a = Orchestrator(settings("UAE")).analyze(profile)
    assert a.compliance.can_send_to_llm
    assert a.ai_decision.approach in {"Hybrid", "AI-first"}
    assert a.chosen is a.costs.with_ai


def test_no_ai_signals_means_ai_not_needed(profile):
    for s in profile.steps:
        s.input_type, s.requires_judgment, s.needs_language_understanding = "structured", False, False
    a = Orchestrator(settings()).analyze(profile)
    assert a.ai_decision.approach == "Rules-only"
    assert "AI not needed" in a.ai_decision.reason


def test_expensive_ai_falls_back_to_rules(profile):
    pricing = load_pricing()
    pricing["ai"]["models_per_mtok"]["claude-opus-5-5"] = {"input": 4000.0, "output": 20000.0}
    a = Orchestrator(settings("UAE"), pricing).analyze(profile)
    assert a.ai_decision.approach == "Rules-only"
    assert a.costs.with_ai.horizon_value < a.costs.rules_only.horizon_value


def test_cost_math(profile):
    a = Orchestrator(settings()).analyze(profile)
    c = a.costs
    # 4000 txns x 21 min = 1400 h/month at 90 AED
    assert c.current_hours_monthly == pytest.approx(1400)
    assert c.current_monthly_cost == pytest.approx(126_000)
    # Rules-only automates eligibility (4 min, 8% exc), register (5 min, 4%), acknowledgement (1 min, 2%)
    saved_min = 4 * 0.92 + 5 * 0.96 + 1 * 0.98
    assert c.rules_only.labor_saved_monthly == pytest.approx(4000 * saved_min / 60 * 90)
    assert c.rules_only.net_monthly == pytest.approx(c.rules_only.labor_saved_monthly - c.rules_only.running_monthly)
    assert c.rules_only.support_monthly == pytest.approx(c.rules_only.build_cost * 0.2 / 12)
    assert c.rules_only.ai_monthly == 0
    assert "Approve high-value claims" not in c.with_ai.automated_steps
    assert c.rules_only.bots >= 1  # legacy desktop system needs an unattended bot


def test_ai_token_cost_per_step():
    pricing = load_pricing()
    step = Step(name="x", input_type="email")
    # 1500 in x $4/M + 300 out x $20/M
    assert ai_cost_per_step(step, pricing) == pytest.approx(0.012)


def test_pricing_currency_conversion():
    pricing = load_pricing()
    aed = pricing_in(pricing, "AED")
    assert aed["labor"]["build_day_rate"] == pytest.approx(600 / 0.2723)
    assert pricing["labor"]["build_day_rate"] == 600  # original untouched
    with pytest.raises(ValueError):
        pricing_in(pricing, "XYZ")


def test_fit_score_bounds_and_components(profile):
    a = Orchestrator(settings()).analyze(profile)
    assert 0 <= a.fit.total <= 100
    assert [c.name for c in a.fit.components] == ["Rules clarity", "Volume", "Stability", "Data quality", "ROI"]
    assert all(0 <= c.points <= c.max_points for c in a.fit.components)


def test_delivery_plan(profile):
    a = Orchestrator(settings()).analyze(profile)
    tools = {p.step: p.tool for p in a.step_plans}
    assert tools["Register claim"] == "Power Automate desktop flow (unattended)"
    assert tools["Approve high-value claims"] == "Power Automate cloud flow + Approvals"
    assert tools["Triage claim email"].startswith("Manual task")  # needs AI, but AI is blocked here
    assert tools["Send acknowledgement"] == "Power Automate cloud flow"
    assert a.maintenance.risk in {"Low", "Medium", "High"}
    assert a.effort.size in {"Small", "Medium", "Large"}


def test_incomplete_profile_rejected():
    with pytest.raises(ValueError):
        Orchestrator(settings()).analyze(ProcessProfile(name="x"))
