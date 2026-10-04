"""BPMN, Visio and voice importers (no network, no API key, faster-whisper faked)."""

from __future__ import annotations

import io
import sys
import types
import zipfile
from pathlib import Path

import pytest

from advisor.importers import ImportResult
from advisor.importers import bpmn, voice, vsdx
from advisor.importers.bpmn import parse_bpmn
from advisor.importers.voice import VoiceUnavailable, is_available, transcribe
from advisor.importers.vsdx import parse_vsdx
from advisor.models import ProcessProfile, missing_fields

FIXTURES = Path(__file__).parent / "fixtures"
CLAIMS_ORDER = [
    "Triage claim email in Outlook",
    "Extract data from scanned claim form",
    "Validate policy cover",
    "Approve high-value claim",
    "Request missing information",
    "Register claim in SAP",
    "Notify member of rejection",
    "Send confirmation email",
    "Archive paper originals",
]


@pytest.fixture(scope="module")
def claims() -> ImportResult:
    return parse_bpmn((FIXTURES / "claims.bpmn").read_bytes())


def steps_by_name(result: ImportResult) -> dict:
    return {s.name: s for s in result.update.steps}


def bpmn_doc(body: str, collaboration: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="d">'
        f"{collaboration}{body}</definitions>"
    ).encode()


# ---------------------------------------------------------------- BPMN


def test_bpmn_steps_follow_sequence_flow(claims):
    assert [s.name for s in claims.update.steps] == CLAIMS_ORDER
    assert claims.update.name == "Health insurance claims intake"


def test_bpmn_unreachable_and_loop_notes(claims):
    notes = " ".join(claims.notes)
    assert "added at the end: 'Archive paper originals'" in notes
    assert "1 loop-back flow" in notes
    assert claims.notes[0].startswith("Read 9 step(s) and 3 system(s)")
    assert "handling times" in claims.notes[-1]


def test_bpmn_gateways_become_decision_points(claims):
    steps = steps_by_name(claims)
    assert "Follows decision 'Policy valid?' (branch 'Yes')" in steps["Approve high-value claim"].description
    assert "branch 'No'" in steps["Request missing information"].description
    assert any("Decision point before 'Register claim in SAP': 'Approved?'" in n for n in claims.notes)
    assert "decision" not in (steps["Extract data from scanned claim form"].description or "").lower()


def test_bpmn_task_types_map_to_step_fields(claims):
    steps = steps_by_name(claims)
    approve = steps["Approve high-value claim"]
    assert approve.is_approval is True
    assert approve.description.startswith("Claims above AED 10,000 need manager sign-off.")
    rule = steps["Validate policy cover"]
    assert (rule.rules_documented, rule.requires_judgment, rule.input_type) == (True, False, "structured")
    service = steps["Register claim in SAP"]
    assert (service.input_type, service.requires_judgment, service.is_approval) == ("structured", False, False)
    assert steps["Triage claim email in Outlook"].input_type == "email"
    assert steps["Extract data from scanned claim form"].input_type == "scanned_document"
    assert steps["Send confirmation email"].input_type is None  # sending an email is not reading one
    assert steps["Request missing information"].is_approval is None


def test_bpmn_systems_and_actors(claims):
    steps = steps_by_name(claims)
    assert [s.name for s in claims.update.systems] == ["Outlook", "Legacy Claims System", "SAP"]
    assert steps["Triage claim email in Outlook"].system == "Outlook"
    assert steps["Register claim in SAP"].system == "SAP"  # task name beats the lane
    assert steps["Validate policy cover"].system == "Legacy Claims System"  # system-like lane
    assert "Actor: Claims Clerk." in steps["Triage claim email in Outlook"].description
    assert "'Approve high-value claim' is done by Claims Manager." in claims.notes
    assert steps["Triage claim email in Outlook"].description.startswith("Read the email")
    # No timings or volumes are invented.
    assert all(s.minutes_per_run is None for s in claims.update.steps)
    assert claims.update.transactions_per_month is None


def test_bpmn_update_merges_into_profile(claims):
    profile = ProcessProfile()
    profile.merge(claims.update)
    profile.merge(claims.update)  # importing twice must not duplicate anything
    assert [s.name for s in profile.steps] == CLAIMS_ORDER
    assert len(profile.systems) == 3
    missing = missing_fields(profile)
    assert "step 'Validate policy cover'.minutes_per_run" in missing
    assert "steps (at least 2)" not in missing
    assert "system 'SAP'.has_api_or_connector" in missing


def test_bpmn_update_merges_into_existing_profile(claims):
    profile = ProcessProfile(name="Existing", steps=[{"name": "Approve high-value claim", "minutes_per_run": 2}])
    profile.merge(claims.update)
    approve = profile.steps[0]
    assert approve.minutes_per_run == 2 and approve.is_approval is True
    assert profile.name == "Health insurance claims intake"


def test_bpmn_message_start_with_email_feeds_first_task():
    body = """<process id="p">
      <startEvent id="s" name="Email received"><messageEventDefinition/></startEvent>
      <exclusiveGateway id="g"/>
      <userTask id="a" name="Check details"/>
      <receiveTask id="r" name="Wait for reply e-mail"/>
      <userTask id="b" name="Update record"/>
      <endEvent id="e"/>
      <sequenceFlow id="f1" sourceRef="s" targetRef="g"/>
      <sequenceFlow id="f2" sourceRef="g" targetRef="a"/>
      <sequenceFlow id="f3" sourceRef="a" targetRef="r"/>
      <sequenceFlow id="f4" sourceRef="r" targetRef="b"/>
      <sequenceFlow id="f5" sourceRef="b" targetRef="e"/>
    </process>"""
    steps = steps_by_name(parse_bpmn(bpmn_doc(body)))
    assert steps["Check details"].input_type == "email"
    assert steps["Check details"].description == "Follows decision 'unnamed gateway'."
    assert steps["Wait for reply e-mail"].input_type == "email"
    assert steps["Update record"].input_type is None


def test_bpmn_without_namespace_and_without_start_event():
    data = b"""<definitions><process id="p" name="Invoice handling">
      <task id="b" name="Check invoice"><documentation>  Compare with
      the PO  </documentation></task>
      <task id="a" name="Receive invoice"/>
      <manualTask id="c" name="Sign off payment"/>
      <sequenceFlow id="1" sourceRef="a" targetRef="b"/>
      <sequenceFlow id="2" sourceRef="b" targetRef="c"/>
    </process></definitions>"""
    result = parse_bpmn(data)
    assert [s.name for s in result.update.steps] == ["Receive invoice", "Check invoice", "Sign off payment"]
    assert result.update.name == "Invoice handling"
    assert result.update.steps[1].description == "Compare with the PO"
    assert result.update.steps[2].is_approval is True


def test_bpmn_undeclared_prefix_is_tolerated():
    data = b"""<bpmn2:definitions><bpmn2:process id="p">
      <bpmn2:startEvent id="s"/><bpmn2:task id="a" name="First"/><bpmn2:task id="b" name="Second"/>
      <bpmn2:sequenceFlow id="1" sourceRef="s" targetRef="b"/>
      <bpmn2:sequenceFlow id="2" sourceRef="b" targetRef="a"/>
    </bpmn2:process></bpmn2:definitions>"""
    assert [s.name for s in parse_bpmn(data).update.steps] == ["Second", "First"]


def test_bpmn_multiple_pools_ordered_by_message_flow():
    collab = """<collaboration id="c">
      <participant id="P1" name="Sales" processRef="sales"/>
      <participant id="P2" name="SAP" processRef="erp"/>
      <messageFlow id="m" sourceRef="send" targetRef="s2"/>
    </collaboration>"""
    body = """
    <process id="erp">
      <startEvent id="s2"><messageEventDefinition/></startEvent>
      <serviceTask id="post" name="Post sales order"/>
      <sequenceFlow id="e1" sourceRef="s2" targetRef="post"/>
    </process>
    <process id="sales">
      <startEvent id="s1"/>
      <userTask id="take" name="Take customer order"/>
      <sendTask id="send" name="Send order to ERP"/>
      <sequenceFlow id="f1" sourceRef="s1" targetRef="take"/>
      <sequenceFlow id="f2" sourceRef="take" targetRef="send"/>
    </process>"""
    result = parse_bpmn(bpmn_doc(body, collab))
    assert [s.name for s in result.update.steps] == ["Take customer order", "Send order to ERP", "Post sales order"]
    steps = steps_by_name(result)
    assert steps["Post sales order"].system == "SAP"
    assert "Actor: Sales." in steps["Take customer order"].description  # no lanes: the pool is the actor
    assert "Diagram has 2 pools: SAP, Sales." in result.notes
    assert result.update.name == "Sales"


def test_bpmn_parallel_branches_subprocess_and_boundary_event():
    body = """<process id="p">
      <startEvent id="s"/>
      <parallelGateway id="fork"/>
      <userTask id="a" name="Check credit"/>
      <userTask id="b" name="Check stock"/>
      <parallelGateway id="join"/>
      <subProcess id="sp" name="Ship goods">
        <startEvent id="ss"/><task id="inner" name="Pick items"/>
        <sequenceFlow id="i1" sourceRef="ss" targetRef="inner"/>
      </subProcess>
      <boundaryEvent id="timer" attachedToRef="sp"/>
      <callActivity id="esc" name="Escalate delay"/>
      <endEvent id="e"/>
      <sequenceFlow id="1" sourceRef="s" targetRef="fork"/>
      <sequenceFlow id="2" sourceRef="fork" targetRef="a"/>
      <sequenceFlow id="3" sourceRef="fork" targetRef="b"/>
      <sequenceFlow id="4" sourceRef="a" targetRef="join"/>
      <sequenceFlow id="5" sourceRef="b" targetRef="join"/>
      <sequenceFlow id="6" sourceRef="join" targetRef="sp"/>
      <sequenceFlow id="7" sourceRef="sp" targetRef="e"/>
      <sequenceFlow id="8" sourceRef="timer" targetRef="esc"/>
    </process>"""
    result = parse_bpmn(bpmn_doc(body))
    assert [s.name for s in result.update.steps] == ["Check credit", "Check stock", "Ship goods", "Escalate delay"]
    assert not any("Not connected" in n for n in result.notes)
    assert not any("decision" in (s.description or "").lower() for s in result.update.steps)  # parallel != decision


def test_bpmn_duplicate_and_unnamed_tasks():
    body = """<process id="p">
      <startEvent id="s"/><task id="a" name="Check"/><task id="b" name="check"/><task id="c"/>
      <sequenceFlow id="1" sourceRef="s" targetRef="a"/>
      <sequenceFlow id="2" sourceRef="a" targetRef="b"/>
      <sequenceFlow id="3" sourceRef="b" targetRef="c"/>
    </process>"""
    result = parse_bpmn(bpmn_doc(body))
    assert [s.name for s in result.update.steps] == ["Check", "check (2)", "Unnamed step 1"]
    profile = ProcessProfile()
    profile.merge(result.update)
    assert len(profile.steps) == 3


@pytest.mark.parametrize(
    "data",
    [
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
        b"<definitions><process><task name='&lol2;'/></process></definitions>",
        b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b"<definitions><process><task name='&xxe;'/></process></definitions>",
        b'<!doctype definitions SYSTEM "http://evil.example/x.dtd"><definitions/>',
        '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE d [<!ENTITY a "b">]><definitions/>'.encode("utf-16"),
    ],
    ids=["billion-laughs", "external-entity", "external-dtd", "utf16-doctype"],
)
def test_bpmn_rejects_dtd_and_entities(data):
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse_bpmn(data)


@pytest.mark.parametrize(
    "data, message",
    [
        (b"", "empty"),
        (b"   ", "empty"),
        (b"<definitions><process>", "Could not read"),
        (b"<html><body>not bpmn</body></html>", "No BPMN"),
        (b"<definitions>" + b" " * bpmn.MAX_BYTES + b"</definitions>", "larger than"),
    ],
    ids=["empty", "blank", "malformed", "not-bpmn", "oversize"],
)
def test_bpmn_rejects_bad_input(data, message):
    with pytest.raises(ValueError, match=message):
        parse_bpmn(data)


# ---------------------------------------------------------------- Visio

VISIO_NS = "http://schemas.microsoft.com/office/visio/2012/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
MASTERS = {
    "1": "Start/End",
    "2": "Process",
    "3": "Decision",
    "4": "Dynamic connector",
    "5": "Swimlane",
    "6": "Subprocess",
}


def _shape(sid, master, text="", x=None, y=None, w=1.0, h=0.75, line=False):
    cells = ""
    if x is not None:
        cells += f'<Cell N="PinX" V="{x}"/><Cell N="PinY" V="{y}"/><Cell N="Width" V="{w}"/><Cell N="Height" V="{h}"/>'
    if line:
        cells += '<Cell N="BeginX" V="0"/><Cell N="EndX" V="1"/>'
    master_attr = f' Master="{master}"' if master else ""
    text_xml = f"<Text><cp IX='0'/>{text}</Text>" if text else ""
    return f'<Shape ID="{sid}" Type="Shape"{master_attr}>{cells}{text_xml}</Shape>'


def _page(shapes: list[str], links: list[tuple[str, str, str]]) -> str:
    connects = "".join(
        f'<Connect FromSheet="{c}" FromCell="BeginX" FromPart="9" ToSheet="{a}" ToCell="PinX" ToPart="3"/>'
        f'<Connect FromSheet="{c}" FromCell="EndX" FromPart="12" ToSheet="{b}" ToCell="PinX" ToPart="3"/>'
        for c, a, b in links
    )
    return (
        f'<?xml version="1.0" encoding="utf-8"?><PageContents xmlns="{VISIO_NS}" xmlns:r="{REL_NS}">'
        f"<Shapes>{''.join(shapes)}</Shapes><Connects>{connects}</Connects></PageContents>"
    )


def make_vsdx(pages: list[tuple[str, str]], with_index: bool = True, title: str | None = None) -> bytes:
    """Minimal but realistic .vsdx package: pages + page index/rels + masters + core properties."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("visio/document.xml", f'<?xml version="1.0"?><VisioDocument xmlns="{VISIO_NS}"/>')
        masters = "".join(f'<Master ID="{i}" NameU="{n}" Name="{n}"><Rel r:id="rId{i}"/></Master>' for i, n in MASTERS.items())
        z.writestr("visio/masters/masters.xml", f'<?xml version="1.0"?><Masters xmlns="{VISIO_NS}" xmlns:r="{REL_NS}">{masters}</Masters>')
        if title:
            z.writestr(
                "docProps/core.xml",
                '<?xml version="1.0"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                f'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title></cp:coreProperties>',
            )
        index, rels = "", ""
        for n, (name, xml) in enumerate(pages, start=1):
            z.writestr(f"visio/pages/page{n}.xml", xml)
            index += f'<Page ID="{n - 1}" NameU="{name}" Name="{name}"><PageSheet/><Rel r:id="rId{n}"/></Page>'
            rels += f'<Relationship Id="rId{n}" Type="http://schemas.microsoft.com/visio/2010/relationships/page" Target="page{n}.xml"/>'
        # A background page that must be ignored.
        z.writestr("visio/pages/page99.xml", _page([_shape("1", "2", "Background logo", 1, 1)], []))
        index += '<Page ID="99" NameU="Background" Background="1"><Rel r:id="rId99"/></Page>'
        rels += '<Relationship Id="rId99" Target="page99.xml"/>'
        if with_index:
            z.writestr("visio/pages/pages.xml", f'<?xml version="1.0"?><Pages xmlns="{VISIO_NS}" xmlns:r="{REL_NS}">{index}</Pages>')
            z.writestr("visio/pages/_rels/pages.xml.rels", f'<?xml version="1.0"?><Relationships xmlns="{PKG_REL_NS}">{rels}</Relationships>')
    return buf.getvalue()


def claims_page() -> str:
    # Three horizontal swimlanes: clerk (y 6-8), manager (y 3-5), SAP (y 0-2).
    shapes = [
        _shape("20", "5", "Claims Clerk", 5, 7, 10, 2),
        _shape("21", "5", "Claims Manager", 5, 4, 10, 2),
        _shape("22", "5", "SAP", 5, 1, 10, 2),
        _shape("1", "1", "Start", 0.5, 7),
        _shape("2", "2", "Read claim email", 2, 7),
        _shape("3", "3", "Complete?", 4, 7),
        _shape("4", "2", "Request documents", 4, 7.8, 1, 0.3),
        _shape("5", "2", "Approve claim", 6, 4),
        _shape("6", "2", "Post claim", 8, 1),
        _shape("7", "1", "Done", 9.5, 1),
        _shape("8", "", "Claims intake overview", 5, 9.5),  # floating title, never connected
        _shape("30", "4", line=True),
        _shape("31", "4", line=True),
        _shape("32", "4", "Yes", line=True),
        _shape("33", "4", "No", line=True),
        _shape("34", "4", line=True),
        _shape("35", "4", line=True),
        _shape("36", "4", line=True),
    ]
    links = [("30", "1", "2"), ("31", "2", "3"), ("32", "3", "5"), ("33", "3", "4"), ("34", "4", "2"), ("35", "5", "6"), ("36", "6", "7")]
    return _page(shapes, links)


def archive_page() -> str:
    shapes = [
        _shape("1", "1", "Begin", 1, 1),
        _shape("2", "6", "Archive file in SharePoint", 3, 1),
        _shape("3", "1", "Finish", 5, 1),
        _shape("10", None, line=True),  # unstenciled line, still glued
        _shape("11", None, line=True),
    ]
    return _page(shapes, [("11", "2", "3"), ("10", "1", "2")])


@pytest.fixture(scope="module")
def visio() -> ImportResult:
    return parse_vsdx(make_vsdx([("Intake", claims_page()), ("Archiving", archive_page())], title="Claims intake"))


def test_vsdx_steps_follow_connectors_across_pages(visio):
    assert [s.name for s in visio.update.steps] == [
        "Read claim email",
        "Approve claim",
        "Request documents",
        "Post claim",
        "Archive file in SharePoint",
    ]
    assert visio.update.name == "Claims intake"
    assert "2 pages read: 'Intake', 'Archiving'" in " ".join(visio.notes)
    assert "Background logo" not in [s.name for s in visio.update.steps]
    assert "Claims intake overview" not in [s.name for s in visio.update.steps]  # unconnected text box


def test_vsdx_decisions_lanes_and_systems(visio):
    steps = steps_by_name(visio)
    assert "Follows decision 'Complete?' (branch 'Yes')" in steps["Approve claim"].description
    assert "branch 'No'" in steps["Request documents"].description
    assert "Actor: Claims Clerk." in steps["Read claim email"].description
    assert "Actor: Claims Manager." in steps["Approve claim"].description
    assert steps["Approve claim"].is_approval is True
    assert steps["Read claim email"].input_type == "email"
    assert steps["Post claim"].system == "SAP"  # from the swimlane
    assert steps["Archive file in SharePoint"].system == "SharePoint"
    assert [s.name for s in visio.update.systems] == ["SAP", "SharePoint"]
    assert any("loop-back" in n and "'Intake'" in n for n in visio.notes)


def test_vsdx_merges_into_profile(visio):
    profile = ProcessProfile()
    profile.merge(visio.update)
    assert len(profile.steps) == 5 and len(profile.systems) == 2
    assert "steps (at least 2)" not in missing_fields(profile)


def test_vsdx_checked_in_fixture_matches_generator(visio):
    saved = parse_vsdx((FIXTURES / "claims.vsdx").read_bytes())
    assert saved.update == visio.update


def test_vsdx_without_page_index_uses_page_files():
    result = parse_vsdx(make_vsdx([("A", archive_page()), ("B", claims_page())], with_index=False))
    names = [s.name for s in result.update.steps]
    assert names[0] == "Archive file in SharePoint" and "Read claim email" in names
    assert "Background logo" in names  # without the index the background page can't be told apart
    assert result.update.name is None


def test_vsdx_rejects_non_zip_and_missing_pages():
    with pytest.raises(ValueError, match="not a zip"):
        parse_vsdx(b"definitely not a zip")
    with pytest.raises(ValueError, match="empty"):
        parse_vsdx(b"")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w/>")
    with pytest.raises(ValueError, match="No Visio pages"):
        parse_vsdx(buf.getvalue())


def test_vsdx_rejects_dtd_in_page():
    evil = '<?xml version="1.0"?><!DOCTYPE p [<!ENTITY x "y">]><PageContents><Shapes/></PageContents>'
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse_vsdx(make_vsdx([("Evil", evil)]))


def test_vsdx_zip_limits(monkeypatch):
    # Highly compressible part: tiny on disk, large once decompressed.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("visio/pages/page1.xml", b"<PageContents>" + b" " * 2_000_000 + b"</PageContents>")
    bomb = buf.getvalue()
    assert len(bomb) < 50_000
    monkeypatch.setattr(vsdx, "MAX_TOTAL_BYTES", 1_000_000)
    with pytest.raises(ValueError, match="too much data"):
        parse_vsdx(bomb)
    monkeypatch.setattr(vsdx, "MAX_TOTAL_BYTES", 50_000_000)
    monkeypatch.setattr(vsdx, "MAX_PART_BYTES", 1_000_000)
    with pytest.raises(ValueError, match="too large"):
        parse_vsdx(bomb)

    many = io.BytesIO()
    with zipfile.ZipFile(many, "w") as z:
        for i in range(30):
            z.writestr(f"visio/pages/page{i + 1}.xml", "<PageContents/>")
    monkeypatch.setattr(vsdx, "MAX_ENTRIES", 20)
    with pytest.raises(ValueError, match="too many parts"):
        parse_vsdx(many.getvalue())


def test_vsdx_page_cap_and_traversal_names_are_inert(monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../../evil.xml", "<x/>")  # never extracted, never read
        for i in range(3):
            z.writestr(f"visio/pages/page{i + 1}.xml", archive_page())
    monkeypatch.setattr(vsdx, "MAX_PAGES", 2)
    result = parse_vsdx(buf.getvalue())
    assert any("Only the first 2 of 3 pages" in n for n in result.notes)
    assert [s.name for s in result.update.steps] == ["Archive file in SharePoint", "Archive file in SharePoint (2)"]


# ---------------------------------------------------------------- voice


class FakeSegment:
    def __init__(self, text):
        self.text = text


def fake_whisper(calls: list) -> types.ModuleType:
    module = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, size, device="auto", compute_type="default"):
            calls.append(("init", size, device, compute_type))

        def transcribe(self, path, **kwargs):
            p = Path(path)
            calls.append(("transcribe", p, p.exists(), p.read_bytes()))
            return iter([FakeSegment(" First we read the email."), FakeSegment(""), FakeSegment(" Then  we approve it. ")]), None

    module.WhisperModel = WhisperModel
    return module


def test_voice_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # makes the import fail
    assert is_available() is False
    with pytest.raises(VoiceUnavailable, match="pip install faster-whisper") as err:
        transcribe(b"RIFF....", "memo.wav")
    assert isinstance(err.value, RuntimeError)
    assert "downloads" in str(err.value)


def test_voice_transcribes_with_local_model(monkeypatch):
    calls: list = []
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_whisper(calls))
    monkeypatch.setattr(voice, "_MODELS", {})
    assert is_available() is True
    text = transcribe(b"fake-audio", "../../etc/My Memo.M4A", model_size="tiny")
    assert text == "First we read the email. Then we approve it."
    assert calls[0] == ("init", "tiny", "cpu", "int8")
    _, path, existed, content = calls[1]
    assert existed and content == b"fake-audio"
    assert path.suffix == ".m4a" and "etc" not in path.parts
    assert not path.exists()  # temp file cleaned up
    transcribe(b"again", "noext")
    transcribe(b"again", "x.wav", model_size="tiny")
    assert [c[0] for c in calls].count("init") == 2  # tiny cached, base loaded once
    assert calls[3][1].suffix == ".wav"


def test_voice_cleans_up_when_model_fails(monkeypatch):
    seen: list[Path] = []
    module = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, path, **kwargs):
            seen.append(Path(path))
            raise RuntimeError("decoder failed")

    module.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    monkeypatch.setattr(voice, "_MODELS", {})
    with pytest.raises(RuntimeError, match="decoder failed"):
        transcribe(b"audio")
    assert seen and not seen[0].exists()


@pytest.mark.parametrize("audio, size", [(b"", "base"), (b"x", "../models/evil"), (b"x", "C:\\models"), (b"x", "")])
def test_voice_rejects_bad_input(audio, size):
    with pytest.raises(ValueError):
        transcribe(audio, model_size=size)


if __name__ == "__main__":  # write a sample .vsdx for trying the importer in the app
    out = FIXTURES / "claims.vsdx"
    out.write_bytes(make_vsdx([("Intake", claims_page()), ("Archiving", archive_page())], title="Claims intake"))
    print(f"wrote {out}")
