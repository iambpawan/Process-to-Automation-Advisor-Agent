"""Heuristic free-text / SOP parser (no LLM)."""

import pytest

from advisor.importers import ImportResult
from advisor.importers.text import (
    find_systems,
    input_type_of,
    mentions,
    parse_currency,
    parse_hourly_cost,
    parse_minutes,
    parse_number,
    parse_process_text,
    parse_step,
    parse_volume,
    system_facts,
)
from advisor.models import ProcessProfile

SOP = """Health insurance claims intake
We receive about 4,000 claims per month in the Claims Operations team in Dubai. Staff cost is AED 90 per hour.
1. Triage claim email in Outlook (3 min) - read and classify
2. Extract data from the scanned claim form into SharePoint, takes 6 minutes, 10% exceptions
3. Check member eligibility in the legacy claims system - 4 mins, documented rules
4. Register claim in the legacy claims system 5 min
5. Supervisor approves high-value claims in Teams, 2 min
6. Send acknowledgement email to the member, 1 min
Patient diagnosis codes and Emirates ID are on the form. Rules change every quarter. Error rate is about 6%.
"""


def test_full_sop():
    r = parse_process_text(SOP)
    assert isinstance(r, ImportResult)
    u = r.update
    assert u.name == "Health insurance claims intake"
    assert u.transactions_per_month == 4000
    assert u.hourly_labor_cost == 90 and u.currency == "AED"
    assert u.process_stability == "changes_quarterly"
    assert u.error_rate_pct == 6
    assert u.department == "Claims Operations"
    assert u.industry == "Healthcare insurance"
    names = [s.name for s in u.steps]
    assert names == [
        "Triage claim email",
        "Extract data from the scanned claim form",
        "Check member eligibility",
        "Register claim",
        "Supervisor approves high-value claims",
        "Send acknowledgement email to the member",
    ]
    triage, extract, check, register, approve, ack = u.steps
    assert (triage.minutes_per_run, triage.system, triage.input_type) == (3, "Outlook", "email")
    assert triage.needs_language_understanding is True
    assert (extract.minutes_per_run, extract.system, extract.input_type, extract.exception_rate_pct) == (
        6, "SharePoint", "scanned_document", 10
    )
    assert check.system == "Legacy Claims System" and check.rules_documented is True
    assert register.minutes_per_run == 5
    assert approve.is_approval is True and approve.system == "Microsoft Teams"
    assert ack.input_type is None  # sending an email works from structured data; left for the interview
    systems = {s.name: s for s in u.systems}
    assert set(systems) == {"Outlook", "SharePoint", "Legacy Claims System", "Microsoft Teams"}
    assert systems["Legacy Claims System"].has_api_or_connector is False
    assert systems["Legacy Claims System"].is_legacy_desktop is True
    assert systems["Outlook"].has_api_or_connector is True
    assert (u.data.contains_phi, u.data.contains_pii, u.data.uae_data_residency_required) == (True, True, True)
    assert any("verify with legal" in n for n in r.notes)


def test_result_merges_into_profile():
    profile = ProcessProfile()
    profile.merge(parse_process_text(SOP).update)
    assert len(profile.steps) == 6 and profile.system_by_name("Outlook")


def test_sentence_steps_with_then():
    r = parse_process_text(
        "First we read the email from the customer, then we enter the order in SAP which takes 5 minutes, "
        "then the manager approves it. About 200 a day. $35/hour"
    )
    u = r.update
    assert [s.name for s in u.steps] == ["Read the email from the customer", "Enter the order", "Manager approves it"]
    assert u.steps[1].system == "SAP" and u.steps[1].minutes_per_run == 5
    assert u.steps[2].is_approval
    assert u.transactions_per_month == 4400
    assert u.hourly_labor_cost == 35 and u.currency == "USD"


def test_bullets_and_messy_whitespace():
    text = "\n\n  * Receive request via the HR portal  \n\t- Review the request, decide if appropriate (about 10 min)\n• Approve in ServiceNow\n\n"
    u = parse_process_text(text).update
    assert [s.name for s in u.steps] == ["Receive request", "Review the request", "Approve in ServiceNow"]  # a bare verb keeps its system
    assert u.steps[0].system == "HR Portal"
    assert u.steps[1].requires_judgment is True and u.steps[1].minutes_per_run == 10
    assert u.steps[2].is_approval and u.steps[2].system == "ServiceNow"


def test_no_steps_found_adds_note():
    r = parse_process_text("We process vendor invoices. It is slow.")
    assert r.update.steps is None
    assert any("No step list" in n for n in r.notes)


@pytest.mark.parametrize("text", ["", "   ", "\n\n", "!!!@@@", "1.\n2.\n-"])
def test_garbage_does_not_crash(text):
    r = parse_process_text(text)
    assert isinstance(r, ImportResult)


def test_duplicate_step_names_are_made_unique():
    u = parse_process_text("1. Check data\n2. Check data\n3. Check data").update
    assert [s.name for s in u.steps] == ["Check data", "Check data (2)", "Check data (3)"]


def test_negated_phi_and_judgment():
    u = parse_process_text("1. Key order (2 min), no judgment needed\n2. File it\nThere is no patient data involved.").update
    assert u.data.contains_phi is False
    assert u.steps[0].requires_judgment is False


def test_uae_mention_without_health_data_only_noted():
    r = parse_process_text("Our Dubai office handles 300 orders per month. 1. Take order 2. Ship")
    assert r.update.data is None or r.update.data.uae_data_residency_required is None
    assert any("UAE mentioned" in n for n in r.notes)


def test_explicit_residency_statements():
    assert parse_process_text("Data must stay in the UAE.").update.data.uae_data_residency_required is True
    assert parse_process_text("There is no data residency requirement.").update.data.uae_data_residency_required is False


def test_process_name_does_not_trigger_pii():
    r = parse_process_text("Process name: Vendor onboarding\n1. Receive form\n2. Create vendor")
    assert r.update.name == "Vendor onboarding"
    assert r.update.data is None or r.update.data.contains_pii is None


def test_system_overrides_from_text():
    s = system_facts("Claims Portal", "The Claims Portal has no API and its screens change often. It runs on Citrix.")
    assert s.has_api_or_connector is False and s.ui_change_frequency == "frequent"


@pytest.mark.parametrize(
    "text,expected",
    [("5 min", 5), ("takes 2 minutes", 2), ("1.5 mins", 1.5), ("half an hour", 30), ("2-3 minutes", 2.5),
     ("an hour", 60), ("90 seconds", 1.5), ("1 hour", 60), ("2 hrs", 120), ("a few minutes", 3),
     ("AED 90 per hour", None), ("no time given", None)],
)
def test_parse_minutes(text, expected):
    assert parse_minutes(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [("4000 claims per month", 4000), ("200 a day", 4400), ("50 per week", 216.5), ("12k invoices a year", 1000),
     ("1,200 orders every month", 1200), ("4k requests per month", 4000), ("monthly volume of 300", 300),
     ("AED 500 per month", None), ("5 minutes per day", None), ("nothing", None)],
)
def test_parse_volume(text, expected):
    result = parse_volume(text)
    assert (result[0] if result else None) == expected


@pytest.mark.parametrize(
    "text,expected",
    [("AED 90 per hour", (90, "AED")), ("$35/hour", (35, "USD")), ("120 dirhams an hour", (120, "AED")),
     ("hourly cost is 40 EUR", (40, "EUR")), ("£28 per hr", (28, "GBP")), ("SAR 70/h", (70, "SAR")),
     ("hourly rate 50", (50, None)), ("4000 per month", None)],
)
def test_parse_hourly_cost(text, expected):
    assert parse_hourly_cost(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [("4k", 4000), ("1,200", 1200), ("about 30", 30), ("1.5", 1.5), ("a dozen", 12), ("none", 0), ("lots", None),
     ("2 thousand", 2000)],
)
def test_parse_number(text, expected):
    assert parse_number(text) == expected


def test_lowercase_common_words_are_not_systems():
    assert find_systems("our claims teams discuss team dynamics") == []
    assert find_systems("post it in Teams") == ["Microsoft Teams"]


def test_currency_code_inside_words_is_ignored():
    assert parse_volume("1,200 orders every month") == (1200, "1200 per month")
    assert parse_currency("orders") is None


def test_unicode_and_long_text_are_handled():
    text = "عملية المطالبات\n1. Receive claim 📧 in Outlook – 3 min\n2. Validate claim ✅ (5 min)\n" + "filler words " * 2000
    u = parse_process_text(text).update
    assert [s.name for s in u.steps][:2] == ["Receive claim 📧", "Validate claim ✅"]


def test_parse_currency():
    assert parse_currency("AED 90") == "AED"
    assert parse_currency("$40") == "USD"
    assert parse_currency("40 euros") == "EUR"
    assert parse_currency("nothing here") is None
    assert parse_currency("AED") == "AED"
    assert parse_currency("ten pounds of paper") is None


@pytest.mark.parametrize(
    "text,expected",
    [("read the email", "email"), ("scanned pdf", "scanned_document"), ("fax from clinic", "scanned_document"),
     ("handwritten form", "scanned_document"), ("customer phone call", "voice"), ("free text notes", "unstructured"),
     ("letter from member", "unstructured"), ("fill the web form", "structured"), ("csv export", "structured"),
     ("update the customer's email address", None), ("send email to customer", None), ("do the thing", None)],
)
def test_input_type_of(text, expected):
    assert input_type_of(text) == expected


def test_find_systems():
    assert find_systems("Copy from Excel into SAP using Outlook") == ["Excel", "SAP", "Outlook"]
    assert find_systems("Log it in the legacy claims system") == ["Legacy Claims System"]
    assert find_systems("Enter it in Oracle EBS") == ["Oracle"]
    assert find_systems("Update the record in Guidewire Policy Center") == ["Guidewire Policy Center"]
    assert find_systems("Meet on Monday in Dubai") == []
    assert find_systems("Check the mainframe") == ["Mainframe"]


def test_mentions_negation():
    assert mentions("contains patient data", r"patients?") is True
    assert mentions("no patient data", r"patients?") is False
    assert mentions("nothing", r"patients?") is None
    assert mentions("no patient names, but patient diagnoses are stored", r"patients?") is True


def test_parse_step_single_line():
    s = parse_step("Approve the refund in Dynamics 365 (2 min), 3% are rejected")
    assert s.name == "Approve the refund" and s.system == "Dynamics 365"
    assert s.is_approval and s.minutes_per_run == 2 and s.exception_rate_pct == 3


def test_undocumented_rules():
    s = parse_step("Decide the priority - rules are not documented, it's in their heads")
    assert s.rules_documented is False and s.requires_judgment is True
