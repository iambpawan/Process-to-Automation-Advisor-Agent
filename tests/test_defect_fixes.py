"""Regression tests for the defects found in the keyless-mode review (one test group per defect)."""

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from advisor.agents.guided_interviewer import GuidedInterviewer
from advisor.analysis import load_pricing, pricing_in
from advisor.analysis.compliance import assess
from advisor.config import load_settings
from advisor.importers.bpmn import parse_bpmn
from advisor.importers.text import parse_number, parse_process_text, parse_volume
from advisor.live import azure_prices
from advisor.live.refresh import refresh_pricing
from advisor.llm.base import TextDelta
from advisor.models import DataProfile, ProcessProfile, ProfileUpdate, System
from advisor.orchestrator import Orchestrator
from advisor.report import fill, to_docx

SAMPLE = Path(__file__).resolve().parent.parent / "advisor" / "samples" / "claims_intake.json"


def sample() -> ProcessProfile:
    return ProcessProfile.model_validate_json(SAMPLE.read_text(encoding="utf-8"))


def say(agent: GuidedInterviewer, text: str, history: list | None = None) -> str:
    history = history if history is not None else []
    return "".join(e.text for e in agent.run_turn(history, text) if isinstance(e, TextDelta))


def names(text: str) -> list[str]:
    return [s.name for s in parse_process_text(text).update.steps or []]


# ---- 1. a fresh interviewer on a populated profile applies the first message as a correction ----

def test_fresh_interviewer_on_sample_applies_a_volume_correction_at_once():
    p = sample()
    g = GuidedInterviewer(p)
    reply = say(g, "volume is 5000 a month")
    assert p.transactions_per_month == 5000
    assert "4000" not in reply  # the summary shown right after already reflects the change


def test_fresh_interviewer_retries_the_answer_after_an_llm_failure():
    p = ProcessProfile.model_validate({
        "name": "Invoice approval",
        "steps": [{"name": "Read invoice", "minutes_per_run": 2}, {"name": "Key into SAP", "minutes_per_run": 3}],
        "transactions_per_month": 500,
        "hourly_labor_cost": 30,
        "currency": "USD",
    })
    g = GuidedInterviewer(p)
    reply = say(g, "no, yes, 5%")
    assert "couldn't pick out specific facts" not in reply
    first = p.steps[0]
    assert (first.requires_judgment, first.rules_documented, first.exception_rate_pct) == (False, True, 5.0)


def test_fresh_interviewer_with_steps_does_not_report_a_missing_step_list():
    p = ProcessProfile.model_validate({
        "name": "Invoice approval",
        "steps": [{"name": "Read invoice", "minutes_per_run": 2}, {"name": "Key into SAP", "minutes_per_run": 3}],
    })
    reply = say(GuidedInterviewer(p), "about 2000 a month")
    assert "No step list found" not in reply
    assert p.transactions_per_month == 2000


def test_empty_profile_still_starts_with_a_first_message():
    g = GuidedInterviewer(ProcessProfile())
    say(g, "Vendor onboarding")
    assert g.profile.name == "Vendor onboarding"


# ---- 2/16. greetings and sentences are not process names ----

@pytest.mark.parametrize("text", [
    "Hello", "hi", "Can you help me?", "I want to automate invoice processing", "We process 500 invoices a month",
    "thanks", "help",
])
def test_greeting_or_sentence_is_not_the_process_name(text):
    g = GuidedInterviewer(ProcessProfile())
    say(g, text)
    assert g.profile.name is None
    assert "What do you call this process" in say(g, "")


def test_name_is_asked_again_after_a_greeting_and_then_taken():
    g = GuidedInterviewer(ProcessProfile())
    say(g, "Hello")
    reply = say(g, "Invoice approval")
    assert g.profile.name == "Invoice approval"
    assert "Couldn't read" not in reply


def test_volume_in_a_sentence_is_kept_even_though_it_is_not_a_name():
    g = GuidedInterviewer(ProcessProfile())
    say(g, "We process 500 invoices a month")
    assert g.profile.transactions_per_month == 500
    assert g.profile.name is None


# ---- 3/19. Word export ----

def test_docx_survives_control_characters():
    data = to_docx("# Title\x07\n\n| a | b\x01 |\n|---|---|\n| c\x0b | d\x0c |\n\ntext\x00 here")
    assert zipfile.is_zipfile(io.BytesIO(data))


def test_docx_survives_a_table_with_only_a_separator_row():
    assert zipfile.is_zipfile(io.BytesIO(to_docx("# T\n\n|---|---|\n\nafter")))


def test_pasted_control_character_does_not_break_the_document_download():
    update = parse_process_text("Claims intake\n1. Read the email\x07 in Outlook (3 min)\n2. Key into SAP (5 min)").update
    p = sample()
    p.steps[0].name = update.steps[0].name  # the control character survives the parser
    a = Orchestrator(load_settings(), load_pricing()).analyze(p)
    doc = next(i for i in Orchestrator(load_settings(), load_pricing()).write_document(a, None) if isinstance(i, str))
    assert zipfile.is_zipfile(io.BytesIO(to_docx(doc)))


# ---- 4. title with an inline list, and generic headings ----

def test_title_then_inline_list_keeps_the_first_step():
    u = parse_process_text(
        "Invoice approval: 1. Receive invoice (5 min)\n2. Enter into SAP (10 min)\n3. Manager approves (2 min)"
    ).update
    assert u.name == "Invoice approval"
    assert [s.name for s in u.steps] == ["Receive invoice", "Enter into SAP", "Manager approves"]


@pytest.mark.parametrize("text", [
    "Steps:\n1. Receive invoice (5 min)\n2. Enter into SAP (10 min)",
    "Here are the steps: 1. Receive invoice (5 min) 2. Enter into SAP (10 min)",
    "Procedure\n1. Receive invoice (5 min)\n2. Enter into SAP (10 min)",
])
def test_generic_heading_is_not_the_process_name(text):
    u = parse_process_text(text).update
    assert u.name is None
    assert len(u.steps) == 2


# ---- 5. inline "Step N:" and one sentence per step ----

def test_inline_step_n_form_gives_every_step():
    got = names("Step 1: Collect timesheets from Excel (10 min). Step 2: Validate hours in Workday (5 min). "
                "Step 3: Submit to payroll (3 min).")
    assert got == ["Collect timesheets", "Validate hours", "Submit to payroll"]


def test_sentence_per_step_with_durations():
    u = parse_process_text(
        "Buyer creates PO in SAP (8 min). Budget owner approves (2 min). Procurement emails the vendor (3 min)."
    ).update
    assert [s.minutes_per_run for s in u.steps] == [8, 2, 3]
    assert len(u.steps) == 3


def test_prose_before_inline_steps():
    got = names("We handle 8,000 claims a year. Step 1: scan the claim form. Step 2: enter into the Claims Portal. "
                "Step 3: supervisor review.")
    assert len(got) == 3 and got[0] == "Scan the claim form"


# ---- 6. thousands separators and system phrases in step names ----

def test_step_name_keeps_thousands_separator():
    got = names("1. Read email (2 min)\n2. Manager approves invoices over 5,000 AED (3 min)\n3. Post in SAP (2 min)")
    assert got[1] == "Manager approves invoices over 5,000 AED"
    assert got[2] == "Post in SAP"


def test_bare_verb_keeps_its_system_and_family_products_stay_whole():
    u = parse_process_text(
        "1. Create employee record in SAP SuccessFactors (5 min)\n2. Enter into SAP (6 min)\n3. Pay in SAP (4 min)"
    ).update
    assert [s.name for s in u.steps] == ["Create employee record", "Enter into SAP", "Pay in SAP"]
    assert u.steps[0].system == "SAP SuccessFactors"


# ---- 7/20. volume phrasings and error rates ----

@pytest.mark.parametrize("text,expected", [
    ("About 2.5k POs/month.", 2500),
    ("1,200 invoices/month", 1200),
    ("We handle 3,000 tickets monthly", 3000),
    ("around 50 requests daily", 50 * 22),
    ("Every day roughly 300 calls come in.", 300 * 22),
    ("about 80 invoices weekly", 346.4),
    ("approximately 5000 transactions annually", 416.7),
    ("12,000 per annum", 1000),
    ("Peak is 2,000 a month but typically 800 a month", 800),
    ("4000 claims per month", 4000),
])
def test_common_volume_phrasings(text, expected):
    assert parse_volume(text)[0] == pytest.approx(expected)


@pytest.mark.parametrize("text", ["5 days a week", "AED 500 per month", "takes 20 min daily", "3 shifts per day"])
def test_non_volumes_are_still_ignored(text):
    assert parse_volume(text) is None


@pytest.mark.parametrize("text,pct", [
    ("40 per cent of claims need rework", 40), ("10 percent get rejected", 10), ("5% have errors", 5),
])
def test_error_rate_phrasings(text, pct):
    assert parse_process_text(text).update.error_rate_pct == pct


def test_peak_only_figure_is_still_used():
    assert parse_volume("Peak is 2,000 a month")[0] == 2000


# ---- 8. residency alone blocks a non-UAE LLM ----

def test_residency_without_sensitive_data_blocks_a_non_uae_llm():
    p = ProcessProfile(data=DataProfile(contains_phi=False, contains_pii=False, uae_data_residency_required=True))
    r = assess(p, "US")
    assert r.can_send_to_llm is False
    assert "UAE" in r.llm_condition
    assert assess(p, "UAE").can_send_to_llm is True


def test_no_residency_and_no_sensitive_data_still_allows_the_llm():
    p = ProcessProfile(data=DataProfile(contains_phi=False, contains_pii=False, uae_data_residency_required=False))
    assert assess(p, "US").can_send_to_llm is True


# ---- 9. currency casing and aliases ----

@pytest.mark.parametrize("raw,code", [("aed", "AED"), ("usd", "USD"), ("Dh", "AED"), (" eur ", "EUR"), ("dirham", "AED")])
def test_currency_is_normalised(raw, code):
    p = sample()
    p.merge(ProfileUpdate(currency=raw))
    assert p.currency == code
    a = Orchestrator(load_settings(), load_pricing()).analyze(p)
    assert a.currency == code


def test_lowercase_currency_set_directly_still_analyses():
    p = sample()
    p.currency = "aed"  # e.g. loaded from a hand-edited JSON profile
    assert Orchestrator(load_settings(), load_pricing()).analyze(p).currency == "AED"


def test_unknown_currency_message_does_not_blame_the_casing():
    with pytest.raises(ValueError, match="3-letter code"):
        pricing_in(load_pricing(), "XYZ")


# ---- 10/18. descriptive paragraph: facts are not steps ----

HOSPITAL = (
    "Referral letters arrive by email from GPs, often as scanned PDFs. The coordinator reads the referral, then "
    "registers the patient in Cerner, then checks the insurance eligibility on the Daman portal, then books an "
    "appointment with the right specialist, then emails the patient. We get around 600 referrals a month, coordinator "
    "cost is AED 70 an hour. Contains patient health data and Emirates ID, must stay in the UAE. Each referral takes "
    "maybe 20 minutes in total."
)


def test_descriptive_paragraph_keeps_only_action_steps():
    u = parse_process_text(HOSPITAL).update
    assert [s.name for s in u.steps] == [
        "Coordinator reads the referral", "Registers the patient", "Checks the insurance eligibility",
        "Books an appointment with the right specialist", "Emails the patient",
    ]
    assert u.transactions_per_month == 600
    assert u.hourly_labor_cost == 70


def test_time_for_the_whole_item_is_spread_not_made_a_step():
    u = parse_process_text(HOSPITAL).update
    assert sum(s.minutes_per_run for s in u.steps) == pytest.approx(20)


def test_whole_item_time_in_prose_reaches_the_steps_and_is_not_replaced_by_defaults():
    text = "ap clerk types them into oracle, then checks vs PO, then manager signs off in outlook. roughly 800 invoices a month i think, takes 10 mins each"
    p = ProcessProfile()
    g = GuidedInterviewer(p)
    say(g, "Invoice matching")
    say(g, text)
    assert p.total_minutes() == pytest.approx(10)
    for _ in range(12):
        if g._pending is None or g._pending[0] != "step":
            break
        say(g, "skip")
    assert p.total_minutes() == pytest.approx(10)  # skipping other questions keeps the user's time


def test_explicit_step_times_win_over_a_whole_item_time():
    u = parse_process_text("1. Read (2 min)\n2. Key it in\n3. File it\nIt takes about 10 minutes in total.").update
    assert [s.minutes_per_run for s in u.steps] == [2, 4, 4]


# ---- 11. correcting the system of a step ----

def _invoice_agent() -> GuidedInterviewer:
    p = ProcessProfile()
    g = GuidedInterviewer(p)
    say(g, "Invoice approval")
    say(g, "1. Receive invoice by email in Outlook (2 min) 2. Manager approves in Outlook (1 min), 500 a month, $30 per hour")
    return g


def test_loose_step_wording_changes_the_step_system():
    g = _invoice_agent()
    reply = say(g, "actually the manager approval is in Teams not outlook")
    step = next(s for s in g.profile.steps if s.name == "Manager approves")
    assert step.system == "Microsoft Teams"
    assert g.profile.steps[0].system == "Outlook"
    assert "Microsoft Teams" in [s.name for s in g.profile.systems]
    assert "Updated" in reply


def test_happens_in_phrasing_is_accepted():
    g = _invoice_agent()
    say(g, "the manager approval step happens in Teams")
    assert next(s for s in g.profile.steps if s.name == "Manager approves").system == "Microsoft Teams"


def test_a_system_no_step_uses_is_not_reported_as_noted():
    g = _invoice_agent()
    reply = say(g, "We also chat a lot in Teams about these invoices and the whole thing every day")
    assert "Also noted: system 'Microsoft Teams'" not in reply
    assert g.profile.system_by_name("Microsoft Teams") is None


# ---- 12. facts and renames mid-interview ----

def test_volume_statement_is_accepted_during_step_questions():
    g = _invoice_agent()
    assert g._pending[0] == "step"
    reply = say(g, "volume is 5000 a month")
    assert g.profile.transactions_per_month == 5000
    assert "couldn't pick" not in reply


def test_rename_step_command_renames():
    g = _invoice_agent()
    reply = say(g, "rename step Manager approves to Finance approval")
    assert [s.name for s in g.profile.steps] == ["Receive invoice by email", "Finance approval"]
    assert "Renamed" in reply


def test_rename_step_with_unknown_step_says_so():
    g = _invoice_agent()
    assert "couldn't find" in say(g, "rename step Nonexistent thing to Other")


# ---- 13. exception rate answers ----

@pytest.mark.parametrize("answer,value", [("none", 0.0), ("no", 0.0), ("rare", 2.0), ("rarely", 2.0), ("never", 0.0), ("often", 25.0)])
def test_words_answer_the_exception_rate_question(answer, value):
    p = ProcessProfile.model_validate({
        "name": "X", "transactions_per_month": 100, "hourly_labor_cost": 20, "currency": "USD",
        "steps": [
            {"name": "A step", "minutes_per_run": 2, "system": "SAP", "input_type": "structured",
             "requires_judgment": False, "rules_documented": True},
            {"name": "B step", "minutes_per_run": 2, "system": "SAP", "input_type": "structured",
             "requires_judgment": False, "rules_documented": True, "exception_rate_pct": 1},
        ],
    })
    g = GuidedInterviewer(p)
    assert g._pending == ("step", "A step")
    say(g, answer)
    assert p.steps[0].exception_rate_pct == value


def test_three_part_answer_with_none_as_exception_rate():
    p = ProcessProfile.model_validate({
        "name": "X", "transactions_per_month": 100, "hourly_labor_cost": 20, "currency": "USD",
        "steps": [{"name": "A step", "minutes_per_run": 2, "system": "SAP", "input_type": "structured"},
                  {"name": "B step", "minutes_per_run": 2, "system": "SAP", "input_type": "structured",
                   "requires_judgment": False, "rules_documented": True, "exception_rate_pct": 1}],
    })
    say(GuidedInterviewer(p), "no, yes, none")
    assert (p.steps[0].requires_judgment, p.steps[0].rules_documented, p.steps[0].exception_rate_pct) == (False, True, 0.0)


# ---- 14. trailing facts do not leak into the last step ----

HR = (
    "HR onboarding: 1. Receive the new hire form 2. Create user account 3. Order laptop in ServiceNow "
    "4. Send welcome checklist by email via Outlook (1 min). About 120 new hires a month. Staff cost is $40 per hour. "
    "Everything is structured form data and the rules are documented; the process rarely changes. "
    "No personal health data, no residency requirement"
)


def test_trailing_facts_do_not_leak_into_the_last_step():
    u = parse_process_text(HR).update
    last = u.steps[-1]
    assert last.description == "Send welcome checklist by email via Outlook (1 min)"
    assert u.transactions_per_month == 120 and u.hourly_labor_cost == 40
    assert "120" not in (u.description or "")


def test_whole_process_statement_reaches_every_step():
    u = parse_process_text(HR).update
    assert all(s.input_type == "structured" for s in u.steps)
    assert all(s.rules_documented is True for s in u.steps)


def test_document_does_not_contain_the_leaked_sentences():
    p = ProcessProfile()
    update = parse_process_text(HR).update
    p.merge(update)
    for s in p.steps:
        s.system = s.system or "SAP"
        s.minutes_per_run = s.minutes_per_run or 2
        s.requires_judgment = False
        s.exception_rate_pct = 1
    p.systems = [System(name=n, has_api_or_connector=True, ui_change_frequency="rare") for n in sorted({s.system for s in p.steps})]
    for s in p.steps:
        s.input_type = s.input_type or "structured"
        s.rules_documented = True
    p.data = DataProfile(contains_phi=False, contains_pii=False, uae_data_residency_required=False)
    p.currency = "USD"
    p.merge(ProfileUpdate(process_stability="stable"))
    a = Orchestrator(load_settings(), load_pricing()).analyze(p)
    doc = next(i for i in Orchestrator(load_settings(), load_pricing()).write_document(a, None) if isinstance(i, str))
    assert "About 120 new hires" not in doc
    assert ". ." not in doc


# ---- 15. regressions on the first-message handling ----

def test_first_message_with_only_a_greeting_is_answered_politely():
    g = GuidedInterviewer(ProcessProfile())
    reply = say(g, "hi")
    assert "Process name: 'Hi'" not in reply
    assert "What do you call this process" in reply


# ---- 17. wording in the generated document ----

def _doc_for(p: ProcessProfile) -> str:
    orch = Orchestrator(load_settings(), load_pricing())
    a = orch.analyze(p)
    return next(i for i in orch.write_document(a, None) if isinstance(i, str))


def test_sign_off_sentence_names_the_step_properly():
    p = sample()
    p.steps[-1].is_approval = True
    doc = _doc_for(p)
    assert "Sign-off is performed in the step" in doc
    assert "Sign-off happens in" not in doc


def test_unknown_system_is_described_as_to_be_confirmed():
    from advisor.analysis.delivery import maintenance_risk

    p = sample()
    p.steps[0].system = "Unknown system"
    p.merge(ProfileUpdate(systems=[{"name": "Unknown system", "has_api_or_connector": False, "ui_change_frequency": "occasional"}]))
    drivers = maintenance_risk(p, False).drivers
    assert any("still to be confirmed" in d and "occasionally" in d for d in drivers)
    assert not any(d.startswith("Unknown system") for d in drivers)


def test_acronym_factor_keeps_its_case_in_the_document():
    from advisor.agents.template_writer import TemplateWriter

    p = sample()
    p.hourly_labor_cost = 1.0
    p.transactions_per_month = 10
    a = Orchestrator(load_settings(), load_pricing()).analyze(p)
    text = TemplateWriter().render(a)
    assert "Improving roi" not in text


# ---- fill(): placeholder variants ----

def test_fill_replaces_spaced_and_lowercase_placeholders():
    a = Orchestrator(load_settings(), load_pricing()).analyze(sample())
    out = fill("## Cost\n{{ COST_TABLE }}\n\n{{cost_table}}\n\n{{ UNKNOWN_THING }}", a)
    assert "{{" not in out
    assert out.count("Net saving / month") >= 2
    assert "Appendix" in out  # the other sections were not mentioned, so they are appended


# ---- 21. live data sanity ----

def _fx_fetch(rates):
    def fetch(url):
        if "er-api" in url:
            return {"result": "success", "base_code": "USD", "rates": rates}
        raise OSError("offline")
    return fetch


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_implausible_live_fx_rates_are_ignored(tmp_path):
    static = load_pricing()
    p, notes = refresh_pricing(static, cache_path=tmp_path / "c.json", force=True, now=NOW,
                               fetch=_fx_fetch({"AED": 0.0001, "EUR": 0.9, "GBP": 1e-9}))
    assert p["fx_to_usd"]["AED"] == static["fx_to_usd"]["AED"]
    assert p["fx_to_usd"]["GBP"] == static["fx_to_usd"]["GBP"]
    assert p["fx_to_usd"]["EUR"] == pytest.approx(1 / 0.9)  # a normal move is accepted
    assert any("implausible" in n for n in notes)


def test_absurd_cached_fx_rate_is_ignored(tmp_path):
    static = load_pricing()
    cache = tmp_path / "c.json"
    refresh_pricing(static, cache_path=cache, force=True, now=NOW, fetch=_fx_fetch({"AED": 3.6725}))
    data = json.loads(cache.read_text(encoding="utf-8"))
    data["fx"]["rates"]["AED"] = 1e308
    cache.write_text(json.dumps(data), encoding="utf-8")
    p, _ = refresh_pricing(static, cache_path=cache, now=NOW, allow_network=False)
    assert p["fx_to_usd"]["AED"] == static["fx_to_usd"]["AED"]


def test_unknown_currency_rate_must_stay_in_absolute_bounds(tmp_path):
    p, _ = refresh_pricing(load_pricing(), cache_path=tmp_path / "c.json", force=True, now=NOW,
                           fetch=_fx_fetch({"AED": 3.6725, "ZZZ": 1e-12}))
    assert "ZZZ" not in p["fx_to_usd"]


# ---- 22. Azure NextPageLink host ----

@pytest.mark.parametrize("link", [
    "https://prices.azure.com.evil.example/steal?x=1", "https://prices.azure.com@evil.example/x",
    "http://prices.azure.com/api/retail/prices?p=2", "https://evilprices.azure.com/x",
])
def test_lookalike_next_page_link_is_not_followed(link):
    calls = []

    def fetch(url):
        calls.append(url)
        return {"Items": [{"a": 1}], "NextPageLink": link}

    azure_prices.fetch_items("x", fetch)
    assert len(calls) == 1


def test_real_next_page_link_is_followed():
    calls = []

    def fetch(url):
        calls.append(url)
        if len(calls) == 1:
            return {"Items": [{"a": 1}], "NextPageLink": "https://prices.azure.com/api/retail/prices?$skip=100"}
        return {"Items": [{"a": 2}]}

    assert len(azure_prices.fetch_items("x", fetch)) == 2 and len(calls) == 2


# ---- 23. deeply nested cache ----

def test_deeply_nested_cache_file_is_treated_as_empty(tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text("[" * 100000, encoding="utf-8")
    p, notes = refresh_pricing(load_pricing(), cache_path=cache, allow_network=False)
    assert p["fx_to_usd"]["AED"] > 0


# ---- 24. BPMN fallback keeps non-UTF-8 text ----

def test_bpmn_with_undeclared_prefix_and_latin1_text():
    xml = ('<?xml version="1.0" encoding="ISO-8859-1"?><bpmn:definitions><bpmn:process id="p">'
           '<bpmn:task id="a" name="Prüfen"/><bpmn:task id="b" name="Freigeben"/></bpmn:process></bpmn:definitions>')
    steps = parse_bpmn(xml.encode("latin-1")).update.steps
    assert steps[0].name == "Prüfen"


def test_bpmn_with_undeclared_prefix_and_utf8_text_still_works():
    xml = ('<?xml version="1.0" encoding="UTF-8"?><bpmn:definitions><bpmn:process id="p">'
           '<bpmn:task id="a" name="Prüfen"/><bpmn:task id="b" name="Freigeben"/></bpmn:process></bpmn:definitions>')
    assert parse_bpmn(xml.encode("utf-8")).update.steps[0].name == "Prüfen"


# ---- 25. scientific notation in volumes ----

@pytest.mark.parametrize("text", ["1e999 claims a month", "1E999 claims a month"])
def test_huge_exponent_is_not_read_as_999(text):
    assert parse_volume(text) is None
    assert parse_process_text("Claims\n1. A thing (1 min)\n2. Another thing (2 min)\n" + text).update.transactions_per_month is None


def test_small_exponent_is_read_as_a_number():
    assert parse_volume("2e3 claims a month")[0] == 2000
    assert parse_number("1e999") != 999
