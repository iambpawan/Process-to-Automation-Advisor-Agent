"""Offline guided interviewer: scripted interviews from an empty profile to a complete, analysable one."""

import json

import pytest

from advisor.agents.base import Notice, ToolFinished, ToolStarted
from advisor.agents.guided_interviewer import (
    MANUAL_SYSTEM,
    STEP_DEFAULTS,
    GuidedInterviewer,
    _flag,
    _match,
    _parse_data_answer,
    _parse_system_answer,
    INPUT_SYNONYMS,
    STABILITY_SYNONYMS,
    UI_SYNONYMS,
)
from advisor.config import Settings
from advisor.llm.base import Message, TextDelta
from advisor.models import ProcessProfile, missing_fields
from advisor.orchestrator import Orchestrator

SETTINGS = Settings("claude", "fake", None, "high", "US", "", "USD")

SOP = """Health insurance claims intake
We receive about 4,000 claims per month in Dubai. Staff cost is AED 90 per hour.
1. Triage claim email in Outlook (3 min) - read and classify
2. Extract data from the scanned claim form into SharePoint, takes 6 minutes, 10% exceptions
3. Check member eligibility in the legacy claims system - 4 mins, documented rules
4. Register claim in the legacy claims system 5 min
5. Supervisor approves high-value claims in Teams, 2 min
Patient diagnosis codes and Emirates ID are on the form.
"""


class Driver:
    def __init__(self, profile: ProcessProfile | None = None):
        self.agent = GuidedInterviewer(profile or ProcessProfile())
        self.history: list[Message] = []
        self.events: list = []

    def say(self, text: str) -> str:
        events = list(self.agent.run_turn(self.history, text))
        self.events += events
        return "".join(e.text for e in events if isinstance(e, TextDelta))

    def until_done(self, answer: str = "skip", limit: int = 60) -> None:
        """Answer every remaining question with `answer`, then confirm."""
        for _ in range(limit):
            if self.agent.complete:
                return
            reply = self.say("yes" if self.agent._awaiting_confirm else answer)
        raise AssertionError(f"Interview did not finish; last reply: {reply}")


def test_greeting_mentions_how_to_start():
    g = GuidedInterviewer(ProcessProfile()).greeting()
    assert "describe the process" in g and "skip" in g


def test_full_scripted_interview_from_empty_profile_then_analysis():
    d = Driver()
    answers = [
        "Invoice matching",
        "receive invoice by email, then match it to the PO in SAP, then the manager approves payment",
        "about 1,200 a month",
        "$35 per hour",
        "3 min, outlook, email, no, yes, 5%",   # step 1
        "4",                                      # step 2: minutes only
        "semi-structured, yes, no, 8%",          # step 2: the rest
        "2 min, Outlook, structured, yes, yes, 0%",  # step 3
        "rare, no",                               # SAP (connector assumed): screens rarely change, not desktop
        "rare",                                   # Outlook screens
        "no, yes, no",                            # data
        "rarely",                                 # stability
    ]
    replies = [d.say(a) for a in answers]
    p = d.agent.profile
    assert p.name == "Invoice matching"
    assert [s.name for s in p.steps] == ["Receive invoice by email", "Match it to the PO", "Manager approves payment"]
    assert p.transactions_per_month == 1200 and p.hourly_labor_cost == 35 and p.currency == "USD"
    s1, s2, s3 = p.steps
    assert (s1.minutes_per_run, s1.system, s1.input_type, s1.requires_judgment, s1.rules_documented, s1.exception_rate_pct) == (
        3, "Outlook", "email", False, True, 5
    )
    assert (s2.minutes_per_run, s2.input_type, s2.requires_judgment, s2.rules_documented, s2.exception_rate_pct) == (
        4, "semi_structured", True, False, 8
    )
    assert s3.is_approval and s3.requires_judgment is True and s3.exception_rate_pct == 0
    sap = p.system_by_name("SAP")
    assert sap.has_api_or_connector is True and sap.ui_change_frequency == "rare" and sap.is_legacy_desktop is False
    assert (p.data.contains_phi, p.data.contains_pii, p.data.uae_data_residency_required) == (False, True, False)
    assert p.process_stability == "stable"
    assert missing_fields(p) == []
    assert "Is this right?" in replies[-1] and not d.agent.complete

    d.say("yes")
    assert d.agent.complete and "Invoice matching" in d.agent.summary
    a = Orchestrator(SETTINGS).analyze(p)
    assert a.profile == p and a.profile is not p  # snapshot, not the live profile


def test_history_stays_valid_alternating_user_assistant():
    d = Driver()
    d.say("Invoice matching")
    d.say("summary")
    assert [m.role for m in d.history] == ["user", "assistant", "user", "assistant"]
    assert all(m.content for m in d.history)


def test_sop_first_message_prefills_most_of_profile():
    d = Driver()
    reply = d.say(SOP)
    p = d.agent.profile
    assert p.name == "Health insurance claims intake"
    assert len(p.steps) == 5
    assert p.transactions_per_month == 4000 and p.hourly_labor_cost == 90 and p.currency == "AED"
    assert p.data.contains_phi and p.data.contains_pii and p.data.uae_data_residency_required
    assert p.system_by_name("Legacy Claims System").has_api_or_connector is False
    assert "picked up" in reply
    started = [e for e in d.events if isinstance(e, ToolStarted)]
    finished = [e for e in d.events if isinstance(e, ToolFinished)]
    assert started and started[0].name == "update_profile"
    assert finished and not finished[0].is_error and "still_missing" in json.loads(finished[0].result)
    # The next question targets the first step with gaps.
    assert "Step 1 of 5" in reply and "Triage claim email" in reply


def test_sop_interview_finishes_with_skips_and_analysis_runs():
    d = Driver()
    d.say(SOP)
    d.until_done("skip")
    p = d.agent.profile
    assert missing_fields(p) == []
    assert d.agent.assumptions  # defaults were documented
    Orchestrator(SETTINGS).analyze(p)


def test_skip_step_applies_documented_conservative_defaults():
    d = Driver()
    d.say("Claims intake")
    d.say("1. Receive request\n2. Update record")
    d.say("100 a month")
    d.say("AED 90 per hour")
    reply = d.say("don't know")
    step = d.agent.profile.steps[0]
    assert step.minutes_per_run == STEP_DEFAULTS["minutes_per_run"]
    assert step.requires_judgment is True and step.rules_documented is False
    assert step.system == "Unknown system"
    assert d.agent.profile.system_by_name("Unknown system").has_api_or_connector is False
    assert "assume" in reply and "conservative default" in reply
    assert any(isinstance(e, Notice) for e in d.events)


def test_partial_step_answer_follows_up_only_on_missing_parts():
    d = Driver()
    d.say("Claims intake")
    d.say("1. Receive request\n2. Update record")
    d.say("100 a month")
    d.say("AED 90 per hour")
    reply = d.say("It takes about 4 minutes in SAP from a scanned pdf")
    step = d.agent.profile.steps[0]
    assert step.minutes_per_run == 4 and step.system == "SAP" and step.input_type == "scanned_document"
    assert "minutes" not in reply.split("Please tell me")[1]
    assert "human judgment" in reply and "rules written down" in reply


def test_skip_inside_multi_part_answer_defaults_only_that_part():
    d = Driver()
    d.say("Claims intake")
    d.say("1. Receive request\n2. Update record")
    d.say("100 a month")
    d.say("AED 90 per hour")
    d.say("5 min, Outlook, email, not sure, yes, 2%")
    step = d.agent.profile.steps[0]
    assert step.requires_judgment is True  # default
    assert step.rules_documented is True and step.exception_rate_pct == 2


def test_add_and_remove_step_commands():
    d = Driver()
    d.say("Claims intake")
    d.say("1. Receive request\n2. Update record\n3. Notify customer")
    d.say("add step Archive the file in SharePoint, 1 min")
    names = [s.name for s in d.agent.profile.steps]
    assert names[-1] == "Archive the file"
    assert d.agent.profile.steps[-1].system == "SharePoint" and d.agent.profile.steps[-1].minutes_per_run == 1
    reply = d.say("remove step notify")
    assert "Removed step 'Notify customer'" in reply
    assert "Notify customer" not in [s.name for s in d.agent.profile.steps]
    reply = d.say("remove step nonexistent thing")
    assert "couldn't find" in reply


def test_remove_step_drops_unused_placeholder_system():
    d = Driver()
    d.say("Claims intake")
    d.say("1. Receive request in the claims portal\n2. Update record\n3. Notify customer")
    assert d.agent.profile.system_by_name("Claims Portal")
    d.say("remove step receive request")
    assert d.agent.profile.system_by_name("Claims Portal") is None


def test_summary_command_shows_progress_and_reasks():
    d = Driver()
    d.say("Claims intake")
    reply = d.say("status")
    assert "Claims intake" in reply and "Still needed" in reply and "main steps" in reply


def test_extra_facts_in_free_text_are_merged_without_clobbering():
    d = Driver()
    d.say("Claims intake")
    d.say("1. Receive request\n2. Update record")
    d.say("100 a month")
    # Answering the cost question while also mentioning other facts.
    d.say("AED 90 per hour, and by the way we handle patient records and it handles 999 claims per month")
    p = d.agent.profile
    assert p.hourly_labor_cost == 90
    assert p.data.contains_phi is True
    assert p.transactions_per_month == 100  # confirmed answer not clobbered


def test_explicit_correction_overrides_earlier_answer():
    d = Driver()
    d.say("Claims intake")
    d.say("1. Receive request\n2. Update record")
    d.say("100 a month")
    d.say("actually the volume should be 5000 a month")
    assert d.agent.profile.transactions_per_month == 5000


def test_confirmation_no_then_correction_then_yes():
    d = Driver()
    d.say(SOP)
    for _ in range(60):
        if d.agent._awaiting_confirm:
            break
        d.say("skip")
    assert d.agent._awaiting_confirm
    reply = d.say("no")
    assert "what should I change" in reply and not d.agent.complete
    reply = d.say("step register claim takes 7 min")
    assert d.agent.profile.steps[3].minutes_per_run == 7
    assert "Is this right?" in reply
    d.say("Outlook has no API")
    assert d.agent.profile.system_by_name("Outlook").has_api_or_connector is False
    d.say("no, volume is 5000 a month")
    assert d.agent.profile.transactions_per_month == 5000
    d.say("yes")
    assert d.agent.complete and d.agent.summary
    Orchestrator(SETTINGS).analyze(d.agent.profile)


def test_unreadable_answer_gets_a_hint_and_same_question():
    d = Driver()
    d.say("Claims intake")
    d.say("1. Receive request\n2. Update record")
    reply = d.say("hmm")
    assert "couldn't pick an answer" in reply and "per month" in reply
    assert any(isinstance(e, Notice) for e in d.events)


def test_volume_and_cost_answer_formats():
    d = Driver()
    d.say("Claims intake")
    d.say("Receive request, update record")
    d.say("4k")
    assert d.agent.profile.transactions_per_month == 4000
    d.say("40 EUR")
    assert d.agent.profile.hourly_labor_cost == 40 and d.agent.profile.currency == "EUR"


def test_volume_per_day_and_week_answers():
    for answer, expected in [("200 a day", 4400), ("50 per week", 216.5), ("1,200", 1200), ("about 30", 30)]:
        d = Driver()
        d.say("Claims intake")
        d.say("Receive request, update record")
        d.say(answer)
        assert d.agent.profile.transactions_per_month == expected, answer


def test_manual_system_answer():
    d = Driver()
    d.say("Claims intake")
    d.say("1. Receive request\n2. Update record")
    d.say("100 a month")
    d.say("AED 90 per hour")
    d.say("3 min, paper, structured, no, yes, 1%")
    step = d.agent.profile.steps[0]
    assert step.system == MANUAL_SYSTEM
    assert d.agent.profile.system_by_name(MANUAL_SYSTEM).ui_change_frequency == "rare"


def test_short_first_message_becomes_name():
    d = Driver()
    d.say("Vendor onboarding")
    assert d.agent.profile.name == "Vendor onboarding"


def test_complete_profile_goes_straight_to_confirmation():
    from pathlib import Path

    sample = Path(__file__).resolve().parent.parent / "advisor" / "samples" / "claims_intake.json"
    profile = ProcessProfile.model_validate_json(sample.read_text(encoding="utf-8"))
    d = Driver(profile)
    reply = d.say("Here is our process, already filled in.")
    assert "Is this right?" in reply
    d.say("yes")
    assert d.agent.complete


# ---------- answer parsers ----------

@pytest.mark.parametrize(
    "text,expected",
    [("rarely", "stable"), ("every month", "changes_monthly"), ("a few times a year", "changes_quarterly"),
     ("changes quarterly", "changes_quarterly"), ("stable", "stable"), ("every few months", "changes_quarterly"),
     ("all the time", "changes_monthly"), ("banana", None)],
)
def test_stability_matching(text, expected):
    assert _match(text, STABILITY_SYNONYMS) == expected


@pytest.mark.parametrize(
    "text,expected",
    [("pdf", "scanned_document"), ("scanned forms", "scanned_document"), ("email", "email"), ("phone calls", "voice"),
     ("free text notes", "unstructured"), ("semi-structured", "semi_structured"), ("structured", "structured"),
     ("spreadsheet", "structured"), ("unstructured", "unstructured"), ("invoices", "semi_structured")],
)
def test_input_type_matching(text, expected):
    assert _match(text, INPUT_SYNONYMS) == expected


@pytest.mark.parametrize("text,expected", [("rare", "rare"), ("rarely", "rare"), ("every month", "frequent"),
                                           ("sometimes", "occasional"), ("often", "frequent")])
def test_ui_matching(text, expected):
    assert _match(text, UI_SYNONYMS) == expected


@pytest.mark.parametrize(
    "text,expected",
    [("needs judgment", True), ("no judgment", False), ("judgment: no", False), ("judgment yes", True),
     ("doesn't need judgment", False), ("nothing here", None)],
)
def test_flag(text, expected):
    assert _flag(text, r"judg\w*") is expected


def test_data_answer_variants():
    q = ["contains_phi", "contains_pii", "uae_data_residency_required"]
    assert _parse_data_answer("yes, no, yes", q)[0] == dict(zip(q, [True, False, True]))
    assert _parse_data_answer("none", q)[0] == dict(zip(q, [False, False, False]))
    assert _parse_data_answer("yes to all", q)[0] == dict(zip(q, [True, True, True]))
    values, defaults = _parse_data_answer("skip", q)
    assert values == dict(zip(q, [True, True, True])) and defaults == q
    values, _ = _parse_data_answer("no patient data, but names and phone numbers; it can't leave the UAE", q)
    assert values == {"contains_phi": False, "contains_pii": True, "uae_data_residency_required": True}
    values, _ = _parse_data_answer("data can be stored outside the country", q)
    assert values == {"uae_data_residency_required": False}


def test_system_answer_variants():
    q = ["has_api_or_connector", "ui_change_frequency", "is_legacy_desktop"]
    assert _parse_system_answer("yes, rare, no", q)[0] == {
        "has_api_or_connector": True, "ui_change_frequency": "rare", "is_legacy_desktop": False
    }
    assert _parse_system_answer("no API, screens change often, it's a citrix desktop app", q)[0] == {
        "has_api_or_connector": False, "ui_change_frequency": "frequent", "is_legacy_desktop": True
    }
    values, defaults = _parse_system_answer("not sure", q)
    assert values == {"has_api_or_connector": False, "ui_change_frequency": "occasional"} and set(defaults) == set(values)


def test_random_messy_input_never_crashes_and_history_alternates():
    import random

    rng = random.Random(7)
    words = ("the a in then next 5 min AED 90 per hour claims Outlook SAP legacy system patient - 1. 2. * , ; % "
             "approve review scanned email no not yes skip summary 200 a day step remove add actually").split()
    for _ in range(20):
        d = Driver()
        for _ in range(15):
            d.say(" ".join(rng.choice(words) for _ in range(rng.randint(0, 25))))
        roles = [m.role for m in d.history]
        assert roles == ["user", "assistant"] * 15
