"""Regression tests for the defects found after integration (one section per defect group)."""

from __future__ import annotations

import io
import json
import socket
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from advisor.agents.guided_interviewer import GuidedInterviewer
from advisor.agents.template_writer import TemplateWriter, _step_detail
from advisor.analysis import load_pricing, pricing_in
from advisor.config import load_settings
from advisor.importers import bpmn as bpmn_mod
from advisor.importers import text as T
from advisor.importers.vsdx import parse_vsdx
from advisor.integrations import load_flows, load_license_csv
from advisor.integrations.licenses import classify_sku
from advisor.live import refresh_pricing
from advisor.llm.base import TextDelta
from advisor.models import ProcessProfile, ProfileUpdate, Step, missing_fields
from advisor.orchestrator import Orchestrator

from test_diagram_import import archive_page, claims_page, make_vsdx
from test_live import NOW, FakeFetch

APP = str(Path(__file__).resolve().parent.parent / "app.py")
SAMPLE = Path(__file__).resolve().parent.parent / "advisor" / "samples" / "claims_intake.json"


def sample() -> ProcessProfile:
    return ProcessProfile.model_validate_json(SAMPLE.read_text(encoding="utf-8"))


def say(g: GuidedInterviewer, text: str) -> str:
    history: list = []
    return "".join(e.text for e in g.run_turn(history, text) if isinstance(e, TextDelta))


# ---------------------------------------------------------------- volume


@pytest.mark.parametrize("text, expected", [
    ("about 2 million claims a year", 2_000_000 / 12),
    ("around 2 million records per year", 2_000_000 / 12),
    ("1.5m per month", 1_500_000),
    ("3 thousand a week", 3000 * T.WEEKS_PER_MONTH),
    ("4k a month", 4000),
    ("4000 claims per month", 4000),
])
def test_volume_understands_multipliers(text, expected):
    n, how = T.parse_volume(text)
    assert n == pytest.approx(expected, rel=1e-3)
    assert "e+" not in how


@pytest.mark.parametrize("text, expected", [
    ("team of 4 people, 5 days a week, about 1,000 claims per month", 1000),
    ("we have 3 shifts per day processing 500 forms per day", 500 * T.WORKING_DAYS_PER_MONTH),
    ("we work 6 days a week and handle 300 claims a day", 300 * T.WORKING_DAYS_PER_MONTH),
])
def test_volume_skips_time_and_staffing_counts(text, expected):
    assert T.parse_volume(text)[0] == pytest.approx(expected)


def test_sop_with_working_days_keeps_the_real_volume():
    u = T.parse_process_text("AP\nOur AP team works 5 days a week and handles about 1,000 invoices per month.\n"
                             "1. Read the invoice\n2. Key it into SAP").update
    assert u.transactions_per_month == 1000


def test_guided_interviewer_records_a_million_volume():
    g = GuidedInterviewer(ProcessProfile(name="Claims intake"))
    say(g, "Read the email, then key it into SAP, then file it")
    reply = say(g, "about 2 million claims a year")
    assert g.profile.transactions_per_month == pytest.approx(2_000_000 / 12, rel=1e-3)
    assert "2000000" in reply and "e+" not in reply


# ---------------------------------------------------------------- minutes / approval / systems


@pytest.mark.parametrize("text, expected", [
    ("takes 1 hour 30 minutes", 90),
    ("Enter data (1h 30m)", 90),
    ("1 hour 15 min", 75),
    ("Respond to customer within 24h (3 min)", 3),
    ("Send acknowledgement within 2 hours", None),
    ("SLA of 4 hours", None),
    ("about 30 bucks an hour", None),
    ("$35 an hour", None),
    ("we pay them 40 an hour", None),
    ("it takes an hour", 60),
    ("5 min", 5),
    ("2-3 min", 2.5),
])
def test_parse_minutes(text, expected):
    assert T.parse_minutes(text) == expected


def test_compound_duration_reaches_the_step():
    u = T.parse_process_text("Loan check\n1. Pull credit report (1 hour 15 min)\n2. Decide the loan").update
    assert u.steps[0].minutes_per_run == 75


@pytest.mark.parametrize("line", [
    "Create employee record in Workday from the signed offer form (6 min)",
    "Scan the signed offer letter",
])
def test_signed_offer_is_not_an_approval(line):
    assert not T.parse_step(line).is_approval


@pytest.mark.parametrize("line", ["Manager signed off the request", "Get sign-off from manager", "Supervisor approves"])
def test_real_approvals_still_detected(line):
    assert T.parse_step(line).is_approval


@pytest.mark.parametrize("line, bogus", [
    ("3. Get sign-off from the Finance Director (2 min)", "Finance Director"),
    ("Receive the request from John via email", "John"),
    ("Check the invoice from Acme Corp in SAP", "Acme Corp"),
    ("Log the claim details in the Finance Manager (5 min)", "Finance Manager"),
])
def test_people_and_roles_are_not_systems(line, bogus):
    assert bogus not in T.find_systems(line)
    assert T.parse_step(line).system != bogus


def test_real_systems_still_found_after_in_and_from():
    assert "Workday" in T.find_systems("Create the record in Workday")
    assert "SAP" in T.find_systems("Check the invoice from Acme Corp in SAP")


# ---------------------------------------------------------------- step splitting / names


def test_greeting_example_on_one_line_gives_separate_steps():
    text = ("1. Read the claim email in Outlook (3 min)  2. Key the data into SAP (5 min) "
            "3. Supervisor approves (2 min), about 4,000 claims a month, staff cost AED 90/hour")
    u = T.parse_process_text(text).update
    assert [s.name for s in u.steps] == ["Read the claim email", "Key the data", "Supervisor approves"]
    assert [s.minutes_per_run for s in u.steps] == [3, 5, 2]
    assert [s.is_approval for s in u.steps] == [None, None, True]
    assert u.transactions_per_month == 4000 and u.hourly_labor_cost == 90 and u.currency == "AED"


def test_inline_steps_after_a_name_prefix():
    u = T.parse_process_text("Invoice approval: 1. Receive invoice email in Outlook 2. Key into Oracle 3. Manager approves").update
    assert u.name == "Invoice approval"
    assert len(u.steps) == 3 and u.steps[2].is_approval


def test_guided_interviewer_takes_inline_greeting_example():
    g = GuidedInterviewer(ProcessProfile())
    say(g, "Claims intake: 1. Read the claim email in Outlook (3 min)  2. Key the data into SAP (5 min) 3. File it (1 min)")
    assert g.profile.name == "Claims intake" and len(g.profile.steps) == 3


def test_numbers_inside_a_sentence_are_not_enumerators():
    assert T._break_inline_enumerators("We handle 2. things and 3. more") == "We handle 2. things and 3. more"


def test_messy_text_becomes_sensible_steps():
    u = T.parse_process_text(
        "so basically AP gets invoices by email from suppliers, someone downloads the pdf, types it into oracle, "
        "then manager approves if over 10k, then payment run. we get like 1500 a month").update
    names = [s.name for s in u.steps]
    assert len(names) == 5
    assert not names[0].lower().startswith("so basically")
    assert names[1] == "Downloads the pdf" and "oracle" in names[2].lower()
    assert u.transactions_per_month == 1500


def test_step_names_do_not_end_on_a_dangling_preposition():
    u = T.parse_process_text(
        "Extract patient details and diagnosis from the scanned referral letter in Epic, "
        "then book the appointment and notify the patient by SMS.").update
    assert [s.name for s in u.steps] == ["Extract patient details and diagnosis", "Book the appointment and notify the patient"]


def test_industry_hospital_with_insurance_checks_is_healthcare():
    u = T.parse_process_text("Referral intake at our hospital\n1. Read referral email\n2. Check insurance eligibility in the payer portal").update
    assert u.industry == "Healthcare"


def test_insurance_company_is_still_insurance():
    u = T.parse_process_text("Claims intake\n1. Read the insurance claim\n2. Check the insurance policy").update
    assert u.industry == "Insurance"


def test_changes_rarely_and_process_wide_facts_apply_to_steps():
    u = T.parse_process_text(
        "HR onboarding\n1. Receive form\n2. Create record\n3. Send welcome email\n"
        "All inputs are structured forms, rules are documented in the onboarding checklist, no judgment needed, "
        "exceptions about 2%. No health data, the process changes rarely.").update
    assert u.process_stability == "stable"
    assert all(s.input_type == "structured" and s.requires_judgment is False and s.rules_documented is True
               and s.exception_rate_pct == 2 for s in u.steps)


def test_process_wide_facts_do_not_override_step_values():
    u = T.parse_process_text(
        "HR\n1. Receive form (email)\n2. Create record\nAll inputs are structured forms.").update
    assert u.steps[0].input_type == "email" and u.steps[1].input_type == "structured"


def test_patient_names_negated_still_marks_pii():
    u = T.parse_process_text(
        "Patient referral intake\n1. Receive referral fax (3 min)\n2. Register patient in Cerner (5 min)\n"
        "3. Book appointment\nWe do not store any patient names outside the hospital.").update
    assert u.data.contains_phi is True and u.data.contains_pii is True


def test_explicit_no_personal_data_is_respected():
    u = T.parse_process_text("Intake\n1. Receive the patient referral\n2. Book\nNo personal data is stored.").update
    assert u.data.contains_pii is False


@pytest.mark.parametrize("text, code", [("QAR 50 per hour", "QAR"), ("50 KWD an hour", "KWD"), ("$40/hour", "USD")])
def test_more_currencies_are_recognised(text, code):
    assert T.parse_currency(text) == code
    assert T.parse_hourly_cost(text)[1] == code


# ---------------------------------------------------------------- performance


@pytest.mark.parametrize("payload", [
    "Claims process. IDs: " + ",".join(str(10000 + i) for i in range(6000)),
    "1" * 60_000,
    ",".join("1" for _ in range(20_000)),
    "1. " * 10_000,
    "5% " * 10_000,
    "9" * 30_000 + " min",
], ids=["ids", "digits", "commas", "enumerators", "percents", "minutes"])
def test_long_digit_heavy_input_is_fast(payload):
    t0 = time.time()
    result = T.parse_process_text(payload)
    assert time.time() - t0 < 3
    assert result.update is not None


def test_long_text_is_truncated_with_a_note():
    result = T.parse_process_text("Intake\n" + "word " * (T.MAX_TEXT_CHARS // 4))
    assert any("longer than" in n for n in result.notes)


def test_long_chat_message_is_fast_and_truncated():
    g = GuidedInterviewer(ProcessProfile())
    t0 = time.time()
    reply = say(g, "Claims intake. Reference IDs: " + ",".join(str(10000 + i) for i in range(3000)))
    assert time.time() - t0 < 3
    assert "longer than" in reply


# ---------------------------------------------------------------- guided interviewer


def test_unknown_step_system_gets_asked_about_not_looped():
    p = sample()
    p.merge(ProfileUpdate(steps=[Step(name="Register claim", system="Claims Portal")]))
    assert missing_fields(p) == ["system 'Claims Portal' details"]
    g = GuidedInterviewer(p)
    g._started = True
    reply = say(g, "summary")
    assert "Claims Portal" in reply and "API" in reply and "Is this right" not in reply
    say(g, "no, rare, no")
    assert not missing_fields(p)
    say(g, "yes")
    assert g.complete


def test_never_asks_for_confirmation_while_facts_are_missing():
    p = sample()
    p.merge(ProfileUpdate(steps=[Step(name="Register claim", system="Claims Portal")]))
    g = GuidedInterviewer(p)
    g._started = True
    for text in ("summary", "yes", "yes"):
        assert "Is this right" not in say(g, text)
    assert not g.complete or not missing_fields(p)


def test_correction_names_a_known_system_without_preposition():
    p = sample()
    g = GuidedInterviewer(p)
    g._started = True
    g.complete = True
    say(g, "Outlook has no API")
    assert p.system_by_name("Outlook").has_api_or_connector is False


def test_volume_reply_to_name_question_is_kept_as_volume():
    g = GuidedInterviewer(ProcessProfile())
    say(g, "Invoice approval\n1. Receive invoice email in Outlook\n2. Key into Oracle\n3. Manager approves in Oracle")
    g.profile.name = None
    g._pending = ("name", None)
    reply = say(g, "2000 a month")
    assert g.profile.name is None and g.profile.transactions_per_month == 2000
    assert "name" in reply.lower()


def test_name_reply_is_confirmed():
    g = GuidedInterviewer(ProcessProfile())
    g._started = True
    g._pending = ("name", None)
    reply = say(g, "Invoice approval")
    assert g.profile.name == "Invoice approval" and "Recorded process name: Invoice approval" in reply


def test_step_facts_are_not_taken_as_a_name():
    g = GuidedInterviewer(ProcessProfile())
    g._started = True
    g._pending = ("name", None)
    say(g, "email, yes, no, 15%")
    assert g.profile.name is None


def test_hourly_pay_rate_during_step_question_goes_to_cost():
    g = GuidedInterviewer(ProcessProfile())
    say(g, "Invoice approval\n1. Receive invoice email in Outlook\n2. Key invoice into Oracle\n3. Manager approves in Oracle\nAbout 1500 invoices a month.")
    say(g, "skip")  # cost default
    reply = say(g, "oh and we pay the clerks about $30 an hour")
    assert g.profile.hourly_labor_cost == 30 and g.profile.currency == "USD"
    assert all(s.minutes_per_run != 60 for s in g.profile.steps)
    assert "staff cost" in reply


def test_qar_cost_is_recorded_with_its_currency():
    g = GuidedInterviewer(ProcessProfile(name="X"))
    say(g, "Read the email, then key it into SAP, then file it")
    say(g, "1000 a month")
    say(g, "QAR 50 per hour")
    assert g.profile.currency == "QAR" and g.profile.hourly_labor_cost == 50
    assert "QAR" in pricing_in(load_pricing(), "QAR")["currency"]


# ---------------------------------------------------------------- document text


def test_document_does_not_repeat_the_source_line_or_the_no_flows_sentence():
    p = ProcessProfile.model_validate_json(SAMPLE.read_text(encoding="utf-8"))
    p.steps[0].description = f"{p.steps[0].name} in Outlook ({p.steps[0].minutes_per_run:g} min)"
    a = Orchestrator(load_settings(), load_pricing()).analyze(p)
    doc = "".join(x.text for x in TemplateWriter().write(a, []) if isinstance(x, TextDelta))
    assert f"**{p.steps[0].name}** (" not in doc
    assert "tenant was not checked" not in doc


def test_step_detail_formats_bpmn_actor_and_decision():
    s = Step(name="Finance director approves request", system="ServiceNow",
             description="Actor: Procurement. Follows decision 'Over 5000?' (branch 'yes').")
    detail = _step_detail(s)
    assert detail == 'performed by Procurement; follows the decision "Over 5000?", branch "yes"'


def test_step_detail_keeps_a_description_that_adds_information():
    s = Step(name="Match invoice", description="Compare the invoice lines against the purchase order and flag price differences")
    assert "purchase order" in _step_detail(s)


# ---------------------------------------------------------------- BPMN title


def test_bpmn_process_name_beats_a_role_pool():
    xml = (
        '<?xml version="1.0"?><definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="d">'
        '<collaboration id="c"><participant id="p" name="Purchase Requests" processRef="Process_PR"/></collaboration>'
        '<process id="Process_PR" name="Purchase request approval"><startEvent id="s"/>'
        '<userTask id="t1" name="Review request"/><userTask id="t2" name="Approve request"/><endEvent id="e"/>'
        '<sequenceFlow id="f1" sourceRef="s" targetRef="t1"/><sequenceFlow id="f2" sourceRef="t1" targetRef="t2"/>'
        '<sequenceFlow id="f3" sourceRef="t2" targetRef="e"/></process></definitions>'
    ).encode()
    assert bpmn_mod.parse_bpmn(xml).update.name == "Purchase request approval"


# ---------------------------------------------------------------- live data


def test_refresh_force_fetches_even_with_a_fresh_cache(tmp_path):
    cache = tmp_path / "c.json"
    refresh_pricing(load_pricing(), cache_path=cache, fetch=FakeFetch(), now=NOW)
    later = NOW.replace(hour=15)
    f = FakeFetch()
    _, _ = refresh_pricing(load_pricing(), cache_path=cache, fetch=f, now=later)
    assert f.calls == []  # a normal call within 24 h uses the cache
    p, _ = refresh_pricing(load_pricing(), cache_path=cache, fetch=f, now=later, force=True)
    assert f.calls and p["_live"]["status"] == "live"


def test_live_fx_keeps_every_valid_currency(tmp_path):
    fx = {"result": "success", "base_code": "USD", "rates": {"USD": 1, "AED": 3.6725, "QAR": 3.64, "XYZ1": 5, "BAD": -2}}
    p, _ = refresh_pricing(load_pricing(), cache_path=tmp_path / "c.json", fetch=FakeFetch(fx=fx), now=NOW)
    assert p["fx_to_usd"]["QAR"] == pytest.approx(1 / 3.64)
    assert "XYZ1" not in p["fx_to_usd"] and "BAD" not in p["fx_to_usd"]
    assert pricing_in(p, "QAR")["currency"] == "QAR"


def _fresh(**sections):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {"version": 1, **{k: {**v, "fetched_at": now} if k == "fx" else v for k, v in sections.items()}}, now


@pytest.mark.parametrize("make", [
    lambda now: {"version": 1, "fx": {"rates": {"AED": 0.27}, "fetched_at": now}},
    lambda now: {"version": 1, "fx": {"rates": {"AED": 0.27}, "source": ["x"], "fetched_at": now}},
    lambda now: {"version": 1, "fx": {"rates": [1, 2], "source": "s", "fetched_at": now}},
    lambda now: {"version": 1, "fx": {"rates": {"AED": "high", "EUR": None}, "source": "s", "fetched_at": now}},
    lambda now: {"version": 1, "azure": ["x"]},
    lambda now: {"version": 1, "azure": {"uaenorth": {"models": {"m": {"input": 1, "output": 2}}, "document_intelligence": [1, 2],
                                                      "source": "s", "fetched_at": now}}},
    lambda now: {"version": 1, "azure": {"uaenorth": {"models": {"m": "x"}, "source": 5, "fetched_at": now}}},
    lambda now: {"version": 1, "azure": {"uaenorth": {"models": {"m": {"input": 1, "output": 2}}, "source": ["x"], "fetched_at": now}}},
    lambda now: {"version": 1, "fx": {"rates": {"AED": 0.27}, "source": "s", "fetched_at": 12}},
])
def test_malformed_cache_never_raises(tmp_path, make):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps(make(now)), encoding="utf-8")
    p, notes = refresh_pricing(load_pricing(), cache_path=cache, allow_network=False)
    assert all(isinstance(s, str) for s in p["_live"]["sources"])
    assert "; ".join(p["_live"]["sources"]) is not None
    assert isinstance(notes, list)


# ---------------------------------------------------------------- licences


@pytest.mark.parametrize("sku", [
    "Power Automate Premium (Trial)", "Power Automate Premium Viral", "Power Automate Premium Developer",
    "Power Automate Process (Trial)",
])
def test_trial_licences_are_not_spare_capacity(sku):
    assert classify_sku(sku) == "other"
    inv, _ = load_license_csv(f"Product,Available\n{sku},25\nPower Automate Premium,3\n")
    assert inv.premium_available == 3 and inv.process_available == 0


def test_paid_premium_still_counts():
    assert classify_sku("Power Automate Premium") == "premium"


def test_licence_csv_with_huge_quoted_field_does_not_crash():
    inv, notes = load_license_csv(b'Product,Available\n"' + b"x" * 200_000 + b'",3\nPower Automate Premium,2\n')
    assert inv.premium_available == 2


def test_truncated_utf16_licence_file_does_not_crash():
    inv, notes = load_license_csv(b"\xff\xfeP\x00r\x00o")
    assert notes


def test_bomless_utf16_licence_file_is_read():
    inv, _ = load_license_csv("Product,Available\nPower Automate Premium,5\n".encode("utf-16-le"))
    assert inv.premium_available == 5
    inv, _ = load_license_csv("Product,Available\nPower Automate Premium,6\n".encode("utf-16-be"))
    assert inv.premium_available == 6


# ---------------------------------------------------------------- uploads


def patch_zip(data: bytes, *, flags: int | None = None, method: int | None = None) -> bytes:
    """Rewrite the flag/method fields of every local and central header."""
    b = bytearray(data)
    i = 0
    while (i := b.find(b"PK\x03\x04", i)) != -1:
        if flags is not None:
            b[i + 6:i + 8] = flags.to_bytes(2, "little")
        if method is not None:
            b[i + 8:i + 10] = method.to_bytes(2, "little")
        i += 4
    i = 0
    while (i := b.find(b"PK\x01\x02", i)) != -1:
        if flags is not None:
            b[i + 8:i + 10] = flags.to_bytes(2, "little")
        if method is not None:
            b[i + 10:i + 12] = method.to_bytes(2, "little")
        i += 4
    return bytes(b)


def zipped(members: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, c in members.items():
            zf.writestr(n, c)
    return buf.getvalue()


FLOW = json.dumps({"properties": {"displayName": "F", "definition": {"triggers": {"m": {"type": "Request"}}, "actions": {}}}})
GUID = "3a9e5c1d-7b2f-4e8a-9c0d-aa11bb22cc33"


@pytest.mark.parametrize("name, data", [
    ("deep.json", b"[" * 100_000 + b"]" * 100_000),
    ("deepclient.json", json.dumps({"clientdata": "[" * 100_000 + "]" * 100_000}).encode()),
    ("pkg.zip", zipped({"manifest.json": '{"resources":[1]}', f"Microsoft.Flow/flows/{GUID}/definition.json": FLOW})),
    ("pkg2.zip", zipped({"manifest.json": "[1,2]", f"Microsoft.Flow/flows/{GUID}/definition.json": FLOW})),
    ("enc.zip", patch_zip(zipped({"a.json": FLOW}), flags=1)),
    ("m99.zip", patch_zip(zipped({"a.json": FLOW}), method=99)),
], ids=["deep", "deep-clientdata", "manifest-list-resources", "manifest-list", "encrypted", "method-99"])
def test_odd_flow_uploads_give_notes_not_errors(name, data):
    flows, notes = load_flows([(name, data)])
    assert isinstance(flows, list) and isinstance(notes, list)


def test_manifest_with_bad_shape_still_reads_the_flow():
    flows, _ = load_flows([("pkg.zip", zipped({"manifest.json": '{"resources":[1]}',
                                                f"Microsoft.Flow/flows/{GUID}/definition.json": FLOW}))])
    assert [f.name for f in flows] == ["F"]


def test_entity_expansion_in_customizations_xml_is_rejected():
    entities = '<!DOCTYPE x [<!ENTITY e0 "AAAAAAAAAA"><!ENTITY e1 "&e0;&e0;&e0;&e0;&e0;&e0;&e0;&e0;&e0;&e0;">]>'
    cust = f'<?xml version="1.0"?>{entities}<ImportExportXml><Workflows><Workflow Name="&e1;">' \
           "<JsonFileName>/Workflows/Big-3A9E5C1D-7B2F-4E8A-9C0D-AA11BB22CC33.json</JsonFileName></Workflow></Workflows></ImportExportXml>"
    data = zipped({"customizations.xml": cust, "Workflows/Big-3A9E5C1D-7B2F-4E8A-9C0D-AA11BB22CC33.json": FLOW})
    flows, notes = load_flows([("sol.zip", data)])
    assert flows and all("AAAA" not in f.name for f in flows)
    assert any("customizations.xml" in n for n in notes)


def test_flow_names_are_clamped():
    big = json.dumps({"properties": {"displayName": "N" * 5000, "description": "D" * 5000,
                                     "definition": {"triggers": {}, "actions": {}}}})
    flows, _ = load_flows([("big.json", big.encode())])
    assert len(flows[0].name) <= 200 and len(flows[0].description) <= 500


def _vsdx() -> bytes:
    return make_vsdx([("Intake", claims_page()), ("Archiving", archive_page())])


def test_vsdx_with_flipped_data_byte_is_a_value_error():
    data = bytearray(_vsdx())
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf:
        info = zf.getinfo("visio/pages/page1.xml")
    start = info.header_offset + 30 + len(info.filename) + len(info.extra)
    for off in range(start, start + min(info.compress_size, 40)):
        data[off] ^= 0xFF
    with pytest.raises(ValueError):
        parse_vsdx(bytes(data))


def test_vsdx_bad_crc_is_a_value_error():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("visio/document.xml", "<VisioDocument/>")
        z.writestr("visio/pages/page1.xml", "<PageContents>" + "a" * 100 + "</PageContents>")
    data = bytearray(buf.getvalue())
    i = data.find(b"aaaa")
    data[i] = ord("b")
    with pytest.raises(ValueError):
        parse_vsdx(bytes(data))


@pytest.mark.parametrize("kw", [{"flags": 1}, {"method": 99}])
def test_vsdx_encrypted_or_unsupported_compression_is_a_value_error(kw):
    with pytest.raises(ValueError):
        parse_vsdx(patch_zip(_vsdx(), **kw))


# ---------------------------------------------------------------- orchestrator / app


def test_assessment_is_a_snapshot_of_the_profile():
    p = sample()
    a = Orchestrator(load_settings(), load_pricing()).analyze(p)
    n = len(a.profile.steps)
    p.steps.pop(0)
    assert len(a.profile.steps) == n


@pytest.fixture
def offline_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "offline")
    for key in ("LLM_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LIVE_CACHE_PATH", str(tmp_path / "live_cache.json"))

    def no_network(*a, **k):
        raise AssertionError("the app tried to use the network")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    return tmp_path


def start() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    return at


def button(at: AppTest, label: str, sidebar: bool = False):
    return next(b for b in (at.sidebar.button if sidebar else at.button) if b.label == label)


def test_chat_edit_replaces_the_stale_analysis(offline_env):
    at = start()
    button(at, "Load sample", sidebar=True).click().run()
    assert at.session_state.assessment is not None  # a complete profile is analysed on sight
    before = len(at.session_state.assessment.profile.steps)
    at.chat_input[0].set_value("remove step Register claim").run()
    assert not at.exception
    assert at.session_state.document is None  # the old document described the old profile
    if missing_fields(at.session_state.profile):
        assert at.session_state.assessment is None
    else:  # the stale figures are dropped and recomputed from the edited profile
        assert len(at.session_state.assessment.profile.steps) == before - 1


def test_chat_message_that_changes_nothing_keeps_the_analysis(offline_env):
    at = start()
    button(at, "Load sample", sidebar=True).click().run()
    first = at.session_state.assessment
    at.chat_input[0].set_value("summary").run()
    assert at.session_state.assessment is first


def test_changing_the_runtime_model_clears_the_analysis(offline_env):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cache = {
        "version": 1,
        "fx": {"rates": {"USD": 1.0, "AED": 0.27}, "source": "test FX", "fetched_at": now},
        "azure": {"uaenorth": {"models": {"azure-openai-gpt-4o (uaenorth)": {"input": 3.0, "output": 12.0}},
                               "source": "test Azure", "fetched_at": now}},
    }
    (offline_env / "live_cache.json").write_text(json.dumps(cache), encoding="utf-8")
    at = start()
    button(at, "Load sample", sidebar=True).click().run()
    assert not at.session_state.assessment.compliance.can_send_to_llm  # default model is not UAE-hosted
    at.sidebar.selectbox(key="runtime_model").set_value("azure-openai-gpt-4o (uaenorth)").run()
    assert not at.exception
    # The old figures are cleared and recomputed for the new model's data region.
    assert at.session_state.assessment.compliance.can_send_to_llm


def test_malformed_live_cache_does_not_stop_the_app(offline_env):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (offline_env / "live_cache.json").write_text(
        json.dumps({"version": 1, "fx": {"rates": {"AED": 0.27}, "fetched_at": now}, "azure": ["x"]}), encoding="utf-8")
    at = start()
    assert any("Live data" in str(h.value) for h in at.sidebar.subheader)
