"""UI tests through the real Streamlit app, in offline mode (no LLM key, no network)."""

import json
import socket
import urllib.request
from pathlib import Path

import pytest
import streamlit as st
from streamlit.proto.ChatInput_pb2 import ChatInput as ChatInputProto
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")

INTERVIEW = [
    "Invoice matching",
    "receive invoice by email, then match it to the PO in SAP, then the manager approves payment",
    "about 1,200 a month",
    "$35 per hour",
    "3 min, outlook, email, no, yes, 5%",
    "4",
    "semi-structured, yes, no, 8%",
    "2 min, Outlook, structured, yes, yes, 0%",
    "rare, no",
    "rare",
    "no, yes, no",
    "rarely",
    "yes",
]

SOP = """Invoice matching
We handle about 1,200 invoices per month. Staff cost is $35 per hour.
1. Read the invoice email in Outlook (3 min)
2. Match the invoice to the PO in SAP, 4 minutes, 8% exceptions
3. Manager approves payment in Teams, 2 min
"""

BPMN = """<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <process name="Invoice matching">
    <task id="t1" name="Read the invoice email"/>
    <task id="t2" name="Match the invoice to the PO"/>
    <sequenceFlow sourceRef="t1" targetRef="t2"/>
  </process>
</definitions>
"""


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """Force offline mode and an empty live cache; fail any attempt to open a network connection."""
    monkeypatch.setenv("LLM_PROVIDER", "offline")
    for key in ("LLM_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LIVE_CACHE_PATH", str(tmp_path / "live_cache.json"))

    def no_network(*args, **kwargs):
        raise AssertionError("the app tried to use the network")

    # (sockets themselves can't be blocked: asyncio on Windows uses a local socketpair)
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


def has_button(at: AppTest, label: str) -> bool:
    return any(b.label == label for b in at.button)


def downloads(at: AppTest) -> list[str]:
    return [el.proto.label for el in at.get("download_button")]


def results_shown(at: AppTest) -> bool:
    """The analysis section only exists once the profile holds every required fact."""
    return bool([m for m in at.main.metric if m.label == "Fit score"])


class FakeUpload:
    """The bits of Streamlit's UploadedFile that the chat-bar import path uses."""

    def __init__(self, name: str, data: bytes, type: str = "") -> None:
        self.name = name
        self.type = type
        self._data = data
        self.size = len(data)

    def getvalue(self) -> bytes:
        return self._data


class FakeSubmission:
    """Stands in for ChatInputValue, which AppTest cannot build with attachments."""

    def __init__(self, text: str = "", files: list | None = None) -> None:
        self.text = text
        self.files = files or []


def submit_once(monkeypatch, value) -> None:
    """Make the next st.chat_input call return `value`, and every later one None.

    AppTest can only set the text of a chat input, so an attachment has to be injected
    at the widget. The one-shot guard stops the app's rerun from submitting it twice.
    """
    seen = {"n": 0}

    def fake_chat_input(*args, **kwargs):
        seen["n"] += 1
        return value if seen["n"] == 1 else None

    monkeypatch.setattr(st, "chat_input", fake_chat_input)


def test_sample_analysis_renders():
    at = start()
    button(at, "Load sample", sidebar=True).click().run()
    assert not at.exception
    # No button to press: a complete profile is analysed straight away.
    assert [m.label for m in at.main.metric] == ["Fit score", "Recommendation", "Payback", "Effort"]
    assert "/ 100" in at.main.metric[0].value
    assert len(at.tabs) == 7  # the analysis tabs; imports no longer use tabs
    assert any("No existing flows provided" in m.value for m in at.markdown)
    assert any("validate before quoting" in c.value for c in at.main.caption)


def test_offline_banner_and_guided_greeting():
    at = start()
    assert any("Offline mode" in i.value for i in at.info)
    assert "paste your SOP" in at.chat_message[0].markdown[0].value
    # Requirement: no analysis UI at all while facts are missing.
    assert not results_shown(at)
    assert not has_button(at, "Re-run analysis")
    assert not has_button(at, "Generate design document")
    assert not at.main.metric


def test_sidebar_shows_what_is_expected():
    at = start()
    # Each readiness group is a compact row carrying its own info icon (help text).
    rows = [m for m in at.sidebar.markdown if m.proto.help]
    assert [t for t in ("Process basics", "Steps", "Systems", "Data")] == [
        t for t in ("Process basics", "Steps", "Systems", "Data") if any(t in r.value for r in rows)
    ]
    assert all(r.proto.help for r in rows)  # an info icon per group
    assert any("0 of" in c.value for c in at.sidebar.caption)
    assert any("What the process is called" in c.value for c in at.sidebar.caption)
    assert at.sidebar.selectbox(key="runtime_model").help


def test_chat_bar_carries_send_stop_attach_and_voice():
    at = start()
    proto = at.chat_input[0].proto
    assert proto.submit_mode == ChatInputProto.SubmitMode.Value("SUBMIT_MODE_STOP")
    assert proto.accept_file == ChatInputProto.AcceptFile.Value("SINGLE")
    assert proto.accept_audio is True  # faster-whisper is installed in the test environment
    assert {".bpmn", ".vsdx", ".txt", ".wav"} <= set(proto.file_type)


def test_full_offline_interview_analysis_and_document():
    at = start()
    for answer in INTERVIEW:
        at.chat_input[0].set_value(answer).run()
        assert not at.exception, answer
    state = at.session_state
    assert state.interviewer.complete
    assert state.profile.name == "Invoice matching" and len(state.profile.steps) == 3
    assert "interview is complete" in at.chat_message[-1].markdown[0].value

    # The results appear with the last answer, not behind a button.
    assert results_shown(at)
    assert at.main.metric[1].value in {"Rules-only", "Hybrid", "AI-first"}
    assert state.assessment is not None

    button(at, "Generate design document").click().run()
    assert not at.exception
    doc = state.document
    assert doc.startswith("# Solution Design: Invoice matching")
    assert "{{" not in doc and "No existing flows provided" in doc
    assert state.document_by == "template writer (offline)"
    assert downloads(at) == ["Download Markdown", "Download Word (.docx)"]
    assert any(m.value.startswith("# Solution Design") for m in at.markdown)

    # Re-running on the same profile keeps the figures.
    before = state.assessment.fit.total
    button(at, "Re-run analysis").click().run()
    assert not at.exception
    assert at.session_state.assessment.fit.total == before


def test_import_sop_text_and_merge_then_interviewer_continues():
    at = start()
    at.text_area(key="import_text").set_value(SOP).run()
    button(at, "Read text").click().run()
    assert not at.exception
    assert at.session_state.pending_import is not None
    assert any("Preview" in m.value for m in at.markdown)
    assert at.session_state.profile.steps == []  # nothing merged before the click

    button(at, "Merge into profile").click().run()
    assert not at.exception
    p = at.session_state.profile
    assert p.name == "Invoice matching"
    assert [s.system for s in p.steps][:2] == ["Outlook", "SAP"]
    assert p.transactions_per_month == 1200 and p.hourly_labor_cost == 35
    assert at.session_state.pending_import is None
    # The guided interviewer picks up from what is still missing.
    last = at.chat_message[-1].markdown[0].value
    assert "Merged the pasted text" in last and "Step 1 of 3" in last
    assert not results_shown(at)

    for _ in range(40):  # skip everything else, then confirm
        reply = at.chat_message[-1].markdown[0].value
        if at.session_state.interviewer.complete:
            break
        at.chat_input[0].set_value("yes" if "Is this right?" in reply else "skip").run()
        assert not at.exception
    assert at.session_state.interviewer.complete
    assert at.session_state.profile.name == "Invoice matching"  # merged facts kept
    assert results_shown(at)


def test_import_keeps_existing_name():
    at = start()
    at.chat_input[0].set_value("Supplier invoices").run()
    at.text_area(key="import_text").set_value(SOP).run()
    button(at, "Read text").click().run()
    button(at, "Merge into profile").click().run()
    assert at.session_state.profile.name == "Supplier invoices"
    assert "Kept the name" in at.chat_message[-1].markdown[0].value


def test_sop_attached_to_the_chat_bar_reaches_the_preview(monkeypatch):
    submit_once(monkeypatch, FakeSubmission(files=[FakeUpload("sop.txt", SOP.encode("utf-8"), "text/plain")]))
    at = start()
    pending = at.session_state.pending_import
    assert pending is not None and pending["source"] == "document sop.txt"
    assert [s.system for s in pending["result"].update.steps][:2] == ["Outlook", "SAP"]
    assert at.session_state.profile.steps == []  # preview first, merge second
    assert any("Preview" in m.value for m in at.markdown)


def test_diagram_attached_to_the_chat_bar_reaches_the_preview(monkeypatch):
    submit_once(monkeypatch, FakeSubmission(files=[FakeUpload("flow.bpmn", BPMN.encode("utf-8"))]))
    at = start()
    pending = at.session_state.pending_import
    assert pending is not None and pending["source"] == "diagram flow.bpmn"
    assert [s.name for s in pending["result"].update.steps] == [
        "Read the invoice email", "Match the invoice to the PO"
    ]


def test_unreadable_attachment_is_reported_not_raised(monkeypatch):
    submit_once(monkeypatch, FakeSubmission(files=[FakeUpload("broken.bpmn", b"not a diagram at all")]))
    at = start()
    assert not at.exception
    assert at.session_state.pending_import is None
    assert at.error, "the failure should be shown in the UI"


def test_recorded_audio_is_transcribed_into_the_preview(monkeypatch):
    from advisor.importers import voice

    monkeypatch.setattr(voice, "transcribe", lambda data, name="audio.wav", **kw: SOP)
    submit_once(monkeypatch, FakeSubmission(files=[FakeUpload("recording.wav", b"RIFFfake", "audio/wav")]))
    at = start()
    pending = at.session_state.pending_import
    assert pending is not None and pending["source"] == "voice recording"
    assert pending["transcript"] == SOP  # shown so the user can check it before merging
    assert pending["result"].update.name == "Invoice matching"
    assert any("Transcript:" in c.value for c in at.main.caption)


def test_voice_unavailable_is_reported(monkeypatch):
    from advisor.importers import voice

    def unavailable(*args, **kwargs):
        raise voice.VoiceUnavailable(voice.INSTALL_HINT)

    monkeypatch.setattr(voice, "transcribe", unavailable)
    submit_once(monkeypatch, FakeSubmission(files=[FakeUpload("recording.wav", b"RIFFfake", "audio/wav")]))
    at = start()
    assert at.session_state.pending_import is None
    assert any("faster-whisper" in e.value for e in at.error)


def test_text_and_attachment_in_one_submission(monkeypatch):
    submit_once(
        monkeypatch,
        FakeSubmission("Supplier invoices", [FakeUpload("sop.txt", SOP.encode("utf-8"), "text/plain")]),
    )
    at = start()
    assert not at.exception
    assert at.session_state.profile.name == "Supplier invoices"  # the text ran the interview turn
    assert at.session_state.pending_import is not None  # the file is waiting for review


def test_fresh_live_cache_is_loaded_without_network(offline):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cache = {
        "version": 1,
        "fx": {"rates": {"USD": 1.0, "AED": 0.27}, "source": "test FX", "fetched_at": now},
        "azure": {"uaenorth": {"models": {"azure-openai-gpt-4o (uaenorth)": {"input": 3.0, "output": 12.0}},
                               "source": "test Azure", "fetched_at": now}},
    }
    (offline / "live_cache.json").write_text(json.dumps(cache), encoding="utf-8")
    at = start()
    assert at.session_state.pricing["_live"]["status"] == "cache"
    box = at.sidebar.selectbox(key="runtime_model")
    assert "azure-openai-gpt-4o (uaenorth)" in box.options
    assert box.value == "claude-opus-5-5"  # default unchanged

    # A UAE-hosted runtime model lets AI run on UAE-resident PHI.
    button(at, "Load sample", sidebar=True).click().run()
    at.sidebar.selectbox(key="runtime_model").set_value("azure-openai-gpt-4o (uaenorth)").run()
    assert not at.exception
    assert at.session_state.assessment.compliance.can_send_to_llm  # recomputed for the new model
