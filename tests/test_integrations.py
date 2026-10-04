import dataclasses
import io
import json
import zipfile
from pathlib import Path

import pytest

from advisor.analysis import load_pricing, pricing_in
from advisor.analysis.cost import compare
from advisor.integrations import FlowInfo, LicenseInventory, find_overlaps, load_flows, load_license_csv
from advisor.integrations import power_automate as pa_mod
from advisor.integrations.licenses import classify_sku
from advisor.models import ProcessProfile

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
RAW_FLOW = FIXTURES / "flows" / "claim_acknowledgement.json"
SAMPLE = ROOT.parent / "advisor" / "samples" / "claims_intake.json"

TRIAGE_GUID = "3a9e5c1d-7b2f-4e8a-9c0d-aa11bb22cc33"
ARCHIVE_GUID = "8d7c6b5a-4f3e-4d2c-8b1a-ffeeddccbbaa"


@pytest.fixture
def profile() -> ProcessProfile:
    return ProcessProfile.model_validate_json(SAMPLE.read_text(encoding="utf-8"))


def _zip(members: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _triage_definition() -> dict:
    """Package-style definition with the legacy $connections parameter syntax."""
    return {
        "name": TRIAGE_GUID,
        "id": f"/providers/Microsoft.Flow/flows/{TRIAGE_GUID}",
        "type": "Microsoft.Flow/flows",
        "properties": {
            "apiId": "/providers/Microsoft.PowerApps/apis/shared_logicflows",
            "displayName": "Copy of flow",  # manifest display name wins
            "definition": {
                "triggers": {
                    "When_a_new_email_arrives_(V3)": {
                        "type": "OpenApiConnectionNotification",
                        "inputs": {
                            "host": {
                                "apiId": "/providers/Microsoft.PowerApps/apis/shared_office365",
                                "connectionName": "shared_office365",
                                "operationId": "OnNewEmailV3",
                            },
                            "parameters": {"folderPath": "Inbox", "fetchOnlyWithAttachment": True},
                        },
                    }
                },
                "actions": {
                    "Create_file": {
                        "type": "ApiConnection",
                        "inputs": {
                            "host": {"connection": {"name": "@parameters('$connections')['shared_sharepointonline_1']['connectionId']"}},
                            "method": "post",
                            "path": "/datasets/claims/files",
                        },
                    },
                    "Start_and_wait_for_an_approval": {
                        "type": "OpenApiConnectionWebhook",
                        "inputs": {"host": {"connectionName": "shared_approvals", "operationId": "StartAndWaitForAnApproval"}},
                    },
                    "Scope_log": {
                        "type": "Scope",
                        "actions": {
                            "Add_a_row": {
                                "type": "OpenApiConnection",
                                "inputs": {"host": {"apiId": "/providers/Microsoft.PowerApps/apis/shared_excelonlinebusiness", "connectionName": "shared_excelonlinebusiness", "operationId": "AddRowV2"}},
                            },
                            "HTTP_notify": {"type": "Http", "inputs": {"method": "POST", "uri": "https://example.invalid/hook"}},
                        },
                    },
                },
            },
            "connectionReferences": {
                "shared_office365": {"connectionName": "shared-office365-1", "source": "Embedded", "id": "/providers/Microsoft.PowerApps/apis/shared_office365", "tier": "NotSpecified"},
                "shared_sharepointonline_1": {"connectionName": "shared-sharepointonl-2", "source": "Embedded", "id": "/providers/Microsoft.PowerApps/apis/shared_sharepointonline", "tier": "NotSpecified"},
                "shared_approvals": {"connectionName": "shared-approvals-3", "source": "Embedded", "id": "/providers/Microsoft.PowerApps/apis/shared_approvals", "tier": "NotSpecified"},
                "shared_excelonlinebusiness": {"connectionName": "shared-excelonlinebu-4", "source": "Embedded", "id": "/providers/Microsoft.PowerApps/apis/shared_excelonlinebusiness", "tier": "NotSpecified"},
            },
        },
    }


def _archive_definition() -> dict:
    return {
        "name": ARCHIVE_GUID,
        "properties": {
            "definition": {
                "triggers": {"Recurrence": {"type": "Recurrence", "recurrence": {"frequency": "Day", "interval": 1}}},
                "actions": {"List_rows": {"type": "OpenApiConnection", "inputs": {"host": {"apiId": "/providers/Microsoft.PowerApps/apis/shared_commondataserviceforapps", "connectionName": "shared_commondataserviceforapps", "operationId": "ListRecords"}}}},
            },
            "connectionReferences": {"shared_commondataserviceforapps": {"id": "/providers/Microsoft.PowerApps/apis/shared_commondataserviceforapps"}},
        },
    }


def package_zip() -> bytes:
    manifest = {
        "schema": "1.0",
        "details": {"displayName": "Claims email triage", "createdTime": "2026-05-01T10:00:00Z", "packageTelemetryId": "x"},
        "resources": {
            TRIAGE_GUID: {
                "id": f"/providers/Microsoft.Flow/flows/{TRIAGE_GUID}",
                "name": TRIAGE_GUID,
                "type": "Microsoft.Flow/flows",
                "suggestedCreationType": "Update",
                "creationType": "Existing, New, Update",
                "details": {"displayName": "Claims email triage to SharePoint"},
                "configurableBy": "User",
                "hierarchy": "Root",
                "dependsOn": ["res-o365"],
            },
            ARCHIVE_GUID: {
                "id": f"/providers/Microsoft.Flow/flows/{ARCHIVE_GUID}",
                "name": ARCHIVE_GUID,
                "type": "Microsoft.Flow/flows",
                "details": {"displayName": "Nightly archive of closed records"},
            },
            "res-o365": {
                "id": "/providers/Microsoft.PowerApps/apis/shared_office365",
                "name": "shared_office365",
                "type": "Microsoft.PowerApps/apis",
                "details": {"displayName": "Office 365 Outlook"},
            },
        },
    }
    return _zip(
        {
            "manifest.json": json.dumps(manifest),
            f"Microsoft.Flow/flows/manifest.json": json.dumps({"packageSchemaVersion": "1.0", "flowAssets": {"assetPaths": [TRIAGE_GUID]}}),
            f"Microsoft.Flow/flows/{TRIAGE_GUID}/definition.json": json.dumps(_triage_definition()),
            f"Microsoft.Flow/flows/{TRIAGE_GUID}/apisMap.json": "{}",
            f"Microsoft.Flow/flows/{TRIAGE_GUID}/connectionsMap.json": "{}",
            f"Microsoft.Flow/flows/{ARCHIVE_GUID}/definition.json": json.dumps(_archive_definition()),
        }
    )


def solution_zip() -> bytes:
    wf_guid = "0B1C2D3E-4F50-6172-8394-A5B6C7D8E9F0"
    clientdata = {
        "properties": {
            "connectionReferences": {
                "shared_commondataserviceforapps": {
                    "runtimeSource": "embedded",
                    "connection": {"connectionReferenceLogicalName": "cr_sharedcommondataserviceforapps_a1"},
                    "api": {"name": "shared_commondataserviceforapps"},
                },
                "shared_teams": {
                    "runtimeSource": "embedded",
                    "connection": {"connectionReferenceLogicalName": "cr_sharedteams_b2"},
                    "api": {"name": "shared_teams"},
                },
            },
            "definition": {
                "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
                "triggers": {
                    "When_a_row_is_added": {
                        "type": "OpenApiConnectionWebhook",
                        "inputs": {"host": {"connectionName": "shared_commondataserviceforapps", "operationId": "SubscribeWebhookTrigger", "apiId": "/providers/Microsoft.PowerApps/apis/shared_commondataserviceforapps"}},
                    }
                },
                "actions": {
                    "Check_eligibility": {
                        "type": "If",
                        "actions": {"Notify_team": {"type": "OpenApiConnection", "inputs": {"host": {"connectionName": "shared_teams", "operationId": "PostMessageToConversation", "apiId": "/providers/Microsoft.PowerApps/apis/shared_teams"}}}},
                        "else": {"actions": {}},
                    }
                },
            },
        },
        "schemaVersion": "1.0.0.0",
    }
    customizations = f"""<?xml version="1.0" encoding="utf-8"?>
<ImportExportXml xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Workflows>
    <Workflow WorkflowId="{{{wf_guid.lower()}}}" Name="Member eligibility check alert">
      <JsonFileName>/Workflows/Membereligibilitycheckalert-{wf_guid}.json</JsonFileName>
      <Type>1</Type>
      <Category>5</Category>
      <Descriptions><Description LanguageCode="1033" description="Posts to Teams when a member eligibility check fails" /></Descriptions>
    </Workflow>
  </Workflows>
</ImportExportXml>"""
    return _zip(
        {
            "[Content_Types].xml": "<Types/>",
            "solution.xml": "<ImportExportXml/>",
            "customizations.xml": customizations,
            f"Workflows/Membereligibilitycheckalert-{wf_guid}.json": json.dumps(clientdata),
            "Workflows/Orphanflow-11111111-2222-3333-4444-555555555555.json": json.dumps({"clientdata": json.dumps(clientdata)}),
        }
    )


# ---------------------------------------------------------------- flows


def test_raw_definition_fixture():
    flows, notes = load_flows([(RAW_FLOW.name, RAW_FLOW.read_bytes())])
    assert notes == []
    [f] = flows
    assert f.name == "Send claim acknowledgement email"
    assert f.trigger == "SharePoint: GetOnNewItems"
    assert f.connectors == ["SharePoint", "Outlook", "Microsoft Teams"]
    # Get_item, Condition(+2 then, +1 else), Foreach(+1), Switch(+1 case, +1 default)
    assert f.action_count == 10
    assert f.source == RAW_FLOW.name
    assert "claim reference" in f.description


def test_package_zip_uses_manifest_names_and_all_connector_syntaxes():
    flows, notes = load_flows([("ClaimsTriage_20260501.zip", package_zip())])
    by_name = {f.name: f for f in flows}
    assert set(by_name) == {"Claims email triage to SharePoint", "Nightly archive of closed records"}
    triage = by_name["Claims email triage to SharePoint"]
    assert triage.trigger == "Outlook: OnNewEmailV3"
    assert triage.connectors == ["Outlook", "SharePoint", "Approvals", "Excel", "HTTP"]
    assert triage.action_count == 5
    archive = by_name["Nightly archive of closed records"]
    assert archive.trigger.startswith("Recurrence")
    assert "Day" in archive.trigger
    assert archive.connectors == ["Dataverse"]
    assert notes == []


def test_solution_zip_reads_customizations_and_clientdata():
    flows, notes = load_flows([("ClaimsSolution_1_0_0_1.zip", solution_zip())])
    by_name = {f.name: f for f in flows}
    alert = by_name["Member eligibility check alert"]
    assert alert.description == "Posts to Teams when a member eligibility check fails"
    assert alert.trigger == "Dataverse: SubscribeWebhookTrigger"
    assert alert.connectors == ["Dataverse", "Microsoft Teams"]
    assert alert.action_count == 2
    assert "Orphanflow" in by_name  # no customizations entry: name from file, GUID stripped
    assert by_name["Orphanflow"].connectors == ["Dataverse", "Microsoft Teams"]


def test_raw_json_variants():
    logic_app = {"triggers": {"manual": {"type": "Request", "kind": "Button"}}, "actions": {"Compose": {"type": "Compose"}}}
    record = {"name": "Record flow", "description": "From Dataverse", "clientdata": json.dumps({"properties": {"definition": logic_app}})}
    listing = {"value": [record, {"not": "a flow"}]}
    flows, notes = load_flows(
        [
            ("bare.json", json.dumps(logic_app).encode()),
            ("records.json", json.dumps(listing).encode("utf-8-sig")),
        ]
    )
    assert [f.name for f in flows] == ["bare", "Record flow"]
    assert flows[0].trigger == "Manual (button)"
    assert flows[0].action_count == 1 and flows[0].connectors == []
    assert flows[1].description == "From Dataverse"


def test_unknown_connector_gets_readable_name():
    d = {"properties": {"definition": {"triggers": {}, "actions": {}}, "connectionReferences": {"shared_my_custom_api": {}}}}
    [f], _ = load_flows([("x.json", json.dumps(d).encode())])
    assert f.connectors == ["My Custom Api"]
    assert f.trigger == "Unknown"


def test_bad_inputs_produce_notes_not_errors():
    flows, notes = load_flows(
        [
            ("broken.json", b"{not json"),
            ("broken.zip", b"PK\x03\x04garbage"),
            ("readme.txt", b"hello"),
            ("empty.zip", _zip({"readme.md": "nothing"})),
        ]
    )
    assert flows == []
    text = " ".join(notes)
    assert "broken.json" in text and "broken.zip" in text and "readme.txt" in text and "empty.zip" in text


def test_zip_safety_limits(monkeypatch):
    monkeypatch.setattr(pa_mod, "MAX_ZIP_MEMBERS", 3)
    many = _zip({f"Workflows/f{i}.json": "{}" for i in range(5)})
    flows, notes = load_flows([("many.zip", many)])
    assert flows == [] and "more than 3 files" in notes[0]

    monkeypatch.setattr(pa_mod, "MAX_ZIP_MEMBERS", 2000)
    bomb = _zip({f"Microsoft.Flow/flows/{TRIAGE_GUID}/definition.json": " " * 2_000_000})
    flows, notes = load_flows([("bomb.zip", bomb)])
    assert flows == [] and any("compression ratio" in n for n in notes)

    monkeypatch.setattr(pa_mod, "MAX_UPLOAD_BYTES", 10)
    flows, notes = load_flows([("big.json", b"x" * 11)])
    assert flows == [] and "larger than" in notes[0]


def test_deep_nesting_is_bounded():
    node: dict = {"type": "Compose"}
    for _ in range(200):
        node = {"type": "Scope", "actions": {"inner": node}}
    d = {"triggers": {}, "actions": {"outer": node}}
    [f], _ = load_flows([("deep.json", json.dumps(d).encode())])
    assert 0 < f.action_count <= pa_mod.MAX_NESTING + 2


# ---------------------------------------------------------------- overlaps


def _flows() -> list[FlowInfo]:
    flows, _ = load_flows(
        [(RAW_FLOW.name, RAW_FLOW.read_bytes()), ("pkg.zip", package_zip()), ("sol.zip", solution_zip())]
    )
    return flows


def test_overlaps_find_existing_flows(profile):
    overlaps = find_overlaps(profile, _flows())
    pairs = {(o.step, o.flow) for o in overlaps}
    assert ("Send acknowledgement", "Send claim acknowledgement email") in pairs
    assert ("Triage claim email", "Claims email triage to SharePoint") in pairs
    assert ("Check member eligibility", "Member eligibility check alert") in pairs
    assert all(0.5 <= o.score <= 1 for o in overlaps)
    assert not any(o.flow == "Nightly archive of closed records" for o in overlaps)
    ack = next(o for o in overlaps if o.step == "Send acknowledgement" and o.flow == "Send claim acknowledgement email")
    assert "acknowledgement" in ack.reason and "Outlook" in ack.reason


def test_overlaps_sorted_and_deterministic(profile):
    flows = _flows()
    first = find_overlaps(profile, flows)
    assert first == find_overlaps(profile, flows)
    assert first == find_overlaps(profile, list(reversed(flows)))  # order of uploads does not matter
    keys = [(-o.score, o.step, o.flow) for o in first]
    assert keys == sorted(keys)


def test_common_domain_words_alone_do_not_match(profile):
    pairs = {(o.step, o.flow) for o in find_overlaps(profile, _flows())}
    # Both mention "claim" and "email" and use Outlook, but triage is not acknowledgement.
    assert ("Triage claim email", "Send claim acknowledgement email") not in pairs
    # "registered" only appears in the flow description.
    assert ("Register claim", "Send claim acknowledgement email") not in pairs
    assert len(pairs) == 3


def test_system_match_raises_score(profile):
    flow = FlowInfo(name="Register claim record", trigger="Manual", connectors=[], source="t")
    base = next(o for o in find_overlaps(profile, [flow]) if o.step == "Register claim")
    flow_sp = dataclasses.replace(flow, connectors=["Legacy Claims System"])
    boosted = next(o for o in find_overlaps(profile, [flow_sp]) if o.step == "Register claim")
    assert boosted.score > base.score


def test_unrelated_flow_has_no_overlap(profile):
    flow = FlowInfo(name="Birthday wishes to staff", trigger="Recurrence", connectors=["Outlook"], source="t",
                    description="Monthly greeting card")
    assert find_overlaps(profile, [flow]) == []


# ---------------------------------------------------------------- licences


def test_licence_csv_fixture():
    inv, notes = load_license_csv((FIXTURES / "licenses.csv").read_bytes())
    assert inv.premium_available == 3
    assert inv.process_available == 2  # Process + unattended RPA add-on
    assert inv.ai_builder_credits_available == 1_000_000
    cats = {r.sku: r.category for r in inv.rows}
    assert cats["Microsoft Power Automate Free"] == "other"
    assert cats["Power Apps Premium"] == "other"
    assert cats["Power Automate per user plan"] == "premium"
    assert any("Spare licences" in n for n in notes)
    assert any("capacity unit" in n for n in notes)


def test_licence_csv_total_minus_assigned_semicolon_and_part_numbers():
    text = (
        "skuPartNumber;prepaidUnits.enabled;consumedUnits\n"
        "POWERAUTOMATE_ATTENDED_RPA;12;10\n"
        "POWERAUTOMATE_UNATTENDED_RPA;3;1\n"
        "FLOW_BUSINESS_PROCESS;1;1\n"
        "FLOW_FREE;10000;50\n"
        "AI Builder credits;500000;100000\n"
    )
    inv, notes = load_license_csv(text)
    assert (inv.premium_available, inv.process_available, inv.ai_builder_credits_available) == (2, 2, 400_000)
    row = inv.rows[0]
    assert (row.total, row.assigned, row.available) == (12, 10, 2)


def test_licence_csv_power_platform_style_and_encodings():
    text = "License\tPurchased\tConsumed\nPower Automate per user with attended RPA plan\t1,000\t998\n"
    inv, _ = load_license_csv(text.encode("utf-16"))
    assert inv.premium_available == 2


def test_licence_csv_unrecognised():
    inv, notes = load_license_csv("foo,bar\n1,2\n")
    assert inv == LicenseInventory()
    assert "not recognised" in notes[0]
    assert load_license_csv(b"")[1] == ["Licence file is empty."]
    _, notes = load_license_csv("Product name,Available\nMicrosoft 365 E3,4\n")
    assert any("No Power Automate" in n for n in notes)


@pytest.mark.parametrize(
    "sku,category",
    [
        ("Power Automate Premium", "premium"),
        ("Power Automate per user plan", "premium"),
        ("Power Automate Process", "process"),
        ("Power Automate per flow plan", "process"),
        ("Power Automate unattended RPA add-on", "process"),
        ("Power Automate Hosted RPA add-on", "process"),
        ("AI Builder capacity add-on T1", "ai_builder"),
        ("CDSAICAPACITY", "ai_builder"),
        ("Power Automate for Office 365", "other"),
        ("Dynamics 365 Sales", "other"),
    ],
)
def test_classify_sku(sku, category):
    assert classify_sku(sku) == category


# ---------------------------------------------------------------- cost with inventory


def _compare(profile, pricing, inventory=None, ai_steps=frozenset({"Triage claim email", "Extract claim form data"})):
    return compare(profile, set(ai_steps), 20, 35, pricing, "USD", inventory=inventory)


def test_cost_without_inventory_is_unchanged(profile):
    pricing = load_pricing()
    a = compare(profile, {"Triage claim email"}, 20, 35, pricing, "USD")
    b = _compare(profile, pricing, None, {"Triage claim email"})
    assert a == b
    assert a.with_ai.license_offset_monthly == 0 and a.with_ai.offset_notes == []
    assert not any("Spare licences" in x for x in a.assumptions)


def test_inventory_offsets_premium_and_bots(profile):
    pricing = load_pricing()
    pa = pricing["power_automate"]
    base = _compare(profile, pricing)
    inv = LicenseInventory(premium_available=5, process_available=10)
    off = _compare(profile, pricing, inv)
    for b, o in ((base.rules_only, off.rules_only), (base.with_ai, off.with_ai)):
        assert o.bots == b.bots and o.bots > 0
        expected = pa["premium_per_user_month"] * pa["flow_owner_licenses"] + b.bots * pa["process_license_per_bot_month"]
        assert o.license_offset_monthly == pytest.approx(expected)
        assert o.pa_license_monthly == pytest.approx(0)
        assert o.net_monthly == pytest.approx(b.net_monthly + expected)
        assert o.labor_saved_monthly == b.labor_saved_monthly
    line = next(x for x in off.assumptions if x.startswith("Spare licences"))
    assert "Premium owner licence" in line and "Process licences" in line
    assert off.assumptions[-1] == pricing["_note"]


def test_partial_bot_coverage(profile):
    pricing = load_pricing()
    pricing["power_automate"]["bot_capacity_minutes_per_month"] = 2000  # force several bots
    base = _compare(profile, pricing)
    assert base.rules_only.bots >= 2
    off = _compare(profile, pricing, LicenseInventory(process_available=1))
    assert off.rules_only.pa_license_monthly == pytest.approx(
        base.rules_only.pa_license_monthly - pricing["power_automate"]["process_license_per_bot_month"]
    )
    assert f"1 of {base.rules_only.bots} bot(s)" in " ".join(off.rules_only.offset_notes)


def test_ai_builder_credits_offset(profile):
    pricing = pricing_in(load_pricing(), "AED")
    pricing["ai"]["engine"] = "ai_builder"
    base = compare(profile, {"Triage claim email", "Extract claim form data"}, 20, 35, pricing, "AED")
    needed = 4000 * 2 * pricing["ai"]["ai_builder"]["credits_per_ai_step"]  # 800,000 credits/month
    full = compare(profile, {"Triage claim email", "Extract claim form data"}, 20, 35, pricing, "AED",
                   inventory=LicenseInventory(ai_builder_credits_available=1_000_000))
    assert full.with_ai.ai_monthly == 0
    assert full.rules_only.ai_monthly == 0
    half = compare(profile, {"Triage claim email", "Extract claim form data"}, 20, 35, pricing, "AED",
                   inventory=LicenseInventory(ai_builder_credits_available=needed // 2))
    assert half.with_ai.ai_monthly == pytest.approx(base.with_ai.ai_monthly / 2)
    assert "AI Builder credits/month" in " ".join(half.with_ai.offset_notes)


def test_ai_builder_credits_ignored_for_llm_engine(profile):
    pricing = load_pricing()
    base = _compare(profile, pricing)
    off = _compare(profile, pricing, LicenseInventory(ai_builder_credits_available=10_000_000))
    assert off.with_ai.ai_monthly == base.with_ai.ai_monthly
    assert any("LLM tokens" in x for x in off.assumptions)


def test_empty_inventory_changes_nothing_but_notes(profile):
    pricing = load_pricing()
    base = _compare(profile, pricing)
    off = _compare(profile, pricing, LicenseInventory())
    assert off.with_ai.net_monthly == base.with_ai.net_monthly
    assert off.rules_only.net_monthly == base.rules_only.net_monthly
    assert any("no spare licences apply" in x for x in off.assumptions)


def test_csv_to_cost_end_to_end(profile):
    inv, _ = load_license_csv((FIXTURES / "licenses.csv").read_text(encoding="utf-8"))
    pricing = load_pricing()
    off = _compare(profile, pricing, inv)
    assert off.with_ai.license_offset_monthly > 0
