"""Read existing Power Automate flows from exported files and find steps they already cover.

Supported uploads (all read in memory, nothing is extracted to disk):
- export package .zip  ("Export > Package (.zip)": Microsoft.Flow/flows/<guid>/definition.json + manifest.json)
- solution .zip        (Workflows/*.json, display names from customizations.xml)
- raw .json            (flow definition, Logic Apps style definition, or Dataverse workflow record with `clientdata`)
"""

from __future__ import annotations

import io
import json
import math
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from advisor.importers.bpmn import safe_xml_root
from advisor.models import ProcessProfile

# Zip safety limits: uploads are untrusted.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_ZIP_MEMBERS = 2000
MAX_MEMBER_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MAX_NESTING = 64
MAX_NAME_CHARS = 200
MAX_DESCRIPTION_CHARS = 500

CONNECTOR_NAMES = {
    "office365": "Outlook",
    "office365users": "Office 365 Users",
    "office365groups": "Office 365 Groups",
    "outlook": "Outlook.com",
    "sharepointonline": "SharePoint",
    "onedriveforbusiness": "OneDrive for Business",
    "onedrive": "OneDrive",
    "teams": "Microsoft Teams",
    "approvals": "Approvals",
    "commondataserviceforapps": "Dataverse",
    "commondataservice": "Dataverse",
    "excelonlinebusiness": "Excel",
    "excelonline": "Excel",
    "planner": "Planner",
    "todo": "Microsoft To Do",
    "flowpush": "Notifications",
    "sendmail": "Mail",
    "sql": "SQL Server",
    "dynamicscrmonline": "Dynamics 365",
    "dynamicsax": "Dynamics 365 Finance and Operations",
    "formrecognizer": "Azure AI Document Intelligence",
    "aibuilder": "AI Builder",
    "uiflow": "Desktop flows",
    "webcontents": "HTTP with Microsoft Entra ID",
    "forms": "Microsoft Forms",
    "salesforce": "Salesforce",
    "servicenow": "ServiceNow",
    "sap": "SAP ERP",
    "sapodata": "SAP OData",
    "azureblob": "Azure Blob Storage",
    "keyvault": "Azure Key Vault",
    "docusign": "DocuSign",
    "adobesign": "Adobe Acrobat Sign",
    "wordonlinebusiness": "Word Online",
    "onenote": "OneNote",
    "rss": "RSS",
    "smtp": "SMTP",
    "ftp": "FTP",
    "sftpwithssh": "SFTP",
    "powerbi": "Power BI",
    "kusto": "Azure Data Explorer",
    "azureopenai": "Azure OpenAI",
    "cognitiveservicestextanalytics": "Azure AI Language",
}

# Built-in action/trigger types that are worth listing as connectors.
BUILTIN_CONNECTORS = {"http": "HTTP", "httpwebhook": "HTTP"}

_API_RE = re.compile(r"(shared_[a-z0-9_\-]+)", re.IGNORECASE)


def _api_name(text: str) -> str | None:
    """'.../apis/shared_sharepointonline' or 'shared_sharepointonline_1' -> 'shared_sharepointonline'."""
    m = _API_RE.search(text)
    return re.sub(r"_\d+$", "", m.group(1).lower()) if m else None


@dataclass
class FlowInfo:
    name: str
    trigger: str
    connectors: list[str] = field(default_factory=list)
    action_count: int = 0
    source: str = ""
    description: str | None = None


@dataclass
class Overlap:
    step: str
    flow: str
    score: float  # 0-1
    reason: str


# ---------------------------------------------------------------- loading


def load_flows(files: list[tuple[str, bytes]]) -> tuple[list[FlowInfo], list[str]]:
    """Parse uploaded (filename, content) pairs into flows plus notes for the user."""
    flows: list[FlowInfo] = []
    notes: list[str] = []
    for filename, data in files:
        if len(data) > MAX_UPLOAD_BYTES:
            notes.append(f"{filename}: skipped, larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
            continue
        try:
            if filename.lower().endswith(".zip") or data[:4] == b"PK\x03\x04":
                found = _load_zip(filename, data, notes)
            elif filename.lower().endswith(".json"):
                found = _load_json_file(filename, data, notes)
            else:
                notes.append(f"{filename}: skipped, expected a .zip export or a .json definition.")
                continue
        except zipfile.BadZipFile:
            notes.append(f"{filename}: not a valid zip file.")
            continue
        except Exception as e:  # untrusted upload: encrypted/odd archives, absurd nesting, unexpected structure
            notes.append(f"{filename}: could not be read ({type(e).__name__}), skipped.")
            continue
        if not found:
            notes.append(f"{filename}: no flow definitions found.")
        flows += found
    return flows, notes


def _load_json_file(filename: str, data: bytes, notes: list[str]) -> list[FlowInfo]:
    obj = _parse_json(data, filename, notes)
    if obj is None:
        return []
    items = obj["value"] if isinstance(obj, dict) and isinstance(obj.get("value"), list) else obj
    items = items if isinstance(items, list) else [items]
    hint = PurePosixPath(filename).stem
    flows = []
    for i, item in enumerate(items):
        flow = _flow_from_obj(item, hint if len(items) == 1 else f"{hint} #{i + 1}", filename)
        if flow:
            flows.append(flow)
    return flows


def _load_zip(filename: str, data: bytes, notes: list[str]) -> list[FlowInfo]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            notes.append(f"{filename}: skipped, more than {MAX_ZIP_MEMBERS} files in the zip.")
            return []
        budget = [MAX_TOTAL_BYTES]

        def read(info: zipfile.ZipInfo) -> bytes | None:
            if info.file_size > MAX_MEMBER_BYTES or info.file_size > budget[0]:
                notes.append(f"{filename}: skipped {info.filename} (too large).")
                return None
            if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                notes.append(f"{filename}: skipped {info.filename} (suspicious compression ratio).")
                return None
            with zf.open(info) as fh:
                content = fh.read(MAX_MEMBER_BYTES + 1)  # do not trust the header size
            if len(content) > MAX_MEMBER_BYTES:
                notes.append(f"{filename}: skipped {info.filename} (too large).")
                return None
            budget[0] -= len(content)
            return content

        names = {i.filename.replace("\\", "/"): i for i in infos if not i.is_dir()}
        package_defs = [n for n in names if re.match(r"(?i)^Microsoft\.Flow/flows/[^/]+/definition\.json$", n)]
        solution_defs = [n for n in names if re.match(r"(?i)^Workflows/[^/]+\.json$", n)]
        if package_defs:
            return _load_package(filename, names, package_defs, read, notes)
        if solution_defs:
            return _load_solution(filename, names, solution_defs, read, notes)
        flows = []
        for n in sorted(names):
            if n.lower().endswith(".json") and PurePosixPath(n).name.lower() not in {"manifest.json"}:
                content = read(names[n])
                if content is not None:
                    flows += _load_json_file(f"{filename}/{n}", content, [])
        return flows


def _load_package(filename, names, defs, read, notes) -> list[FlowInfo]:
    display: dict[str, str] = {}
    manifest = next((names[n] for n in names if n.lower() == "manifest.json"), None)
    if manifest is not None and (raw := read(manifest)) is not None:
        m = _parse_json(raw, f"{filename}/manifest.json", notes)
        resources = m.get("resources") if isinstance(m, dict) else None
        for key, res in (resources.items() if isinstance(resources, dict) else ()):
            if isinstance(res, dict) and res.get("type") == "Microsoft.Flow/flows":
                label = (res.get("details") or {}).get("displayName")
                if label:
                    for k in (key, res.get("name"), str(res.get("id", "")).rsplit("/", 1)[-1]):
                        if k:
                            display[k.lower()] = label
    flows = []
    for n in sorted(defs):
        raw = read(names[n])
        if raw is None:
            continue
        obj = _parse_json(raw, f"{filename}/{n}", notes)
        guid = n.split("/")[2]
        hint = display.get(guid.lower())
        if hint is None and isinstance(obj, dict):
            hint = display.get(str(obj.get("name", "")).lower())
        if hint is None and len(display) == 1 and len(defs) == 1:
            hint = next(iter(display.values()))
        flow = _flow_from_obj(obj, hint or guid, filename, prefer_hint=bool(hint))
        if flow:
            flows.append(flow)
    return flows


def _load_solution(filename, names, defs, read, notes) -> list[FlowInfo]:
    meta: dict[str, tuple[str, str | None]] = {}  # json file name -> (display name, description)
    cust = next((names[n] for n in names if n.lower() == "customizations.xml"), None)
    if cust is not None and (raw := read(cust)) is not None:
        try:
            root = safe_xml_root(raw, "customizations.xml", MAX_MEMBER_BYTES)
            for wf in root.iter("Workflow"):
                path = (wf.findtext("JsonFileName") or "").strip().lstrip("/")
                if path:
                    desc = wf.findtext("Descriptions/Description") or wf.findtext("Description")
                    if desc is None:
                        d = wf.find("Descriptions/Description")
                        desc = d.get("description") if d is not None else None
                    meta[path.lower()] = ((wf.get("Name") or "")[:MAX_NAME_CHARS], desc or None)
        except (ET.ParseError, ValueError):
            notes.append(f"{filename}: customizations.xml could not be read; using file names as flow names.")
    flows = []
    for n in sorted(defs):
        raw = read(names[n])
        if raw is None:
            continue
        obj = _parse_json(raw, f"{filename}/{n}", notes)
        label, desc = meta.get(n.lower(), ("", None))
        stem = PurePosixPath(n).stem
        fallback = re.sub(r"-[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$", "", stem)
        flow = _flow_from_obj(obj, label or fallback, filename, prefer_hint=bool(label))
        if flow:
            if desc and not flow.description:
                flow.description = desc
            flows.append(flow)
    return flows


def _parse_json(data: bytes, label: str, notes: list[str]) -> Any:
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):  # JSONDecodeError is a ValueError
        notes.append(f"{label}: not valid JSON, skipped.")
        return None


# ---------------------------------------------------------------- definition parsing


def _flow_from_obj(obj: Any, hint: str, source: str, prefer_hint: bool = False) -> FlowInfo | None:
    if not isinstance(obj, dict):
        return None
    record_name = record_desc = None
    if isinstance(obj.get("clientdata"), str):  # Dataverse workflow record
        record_name, record_desc = obj.get("name"), obj.get("description")
        try:
            obj = json.loads(obj["clientdata"])
        except (ValueError, RecursionError):
            return None
        if not isinstance(obj, dict):
            return None
    props = obj.get("properties") if isinstance(obj.get("properties"), dict) else obj
    definition = props.get("definition")
    if not isinstance(definition, dict):
        definition = props if ("triggers" in props or "actions" in props) else None
    if definition is None:
        return None

    conn_refs = props.get("connectionReferences") or {}
    conn_refs = conn_refs if isinstance(conn_refs, dict) else {}
    ref_api: dict[str, str] = {}  # connection reference key -> shared_xxx api name
    connectors: list[str] = []
    for key, ref in conn_refs.items():
        api = _api_from_ref(key, ref)
        if api:
            ref_api[key.lower()] = api
            _add(connectors, _connector_label(api))

    triggers = definition.get("triggers") or {}
    actions = definition.get("actions") or {}
    trigger = "Unknown"
    if isinstance(triggers, dict) and triggers:
        trigger = _describe_trigger(next(iter(triggers.values())), ref_api)
    for node in _walk(triggers) + _walk(actions):
        label = _node_connector(node, ref_api)
        if label:
            _add(connectors, label)

    name = props.get("displayName") or obj.get("displayName") or record_name
    if prefer_hint or not name:
        name = hint or name or "Unnamed flow"
    description = definition.get("description") or props.get("description") or record_desc
    return FlowInfo(
        name=str(name)[:MAX_NAME_CHARS],
        trigger=trigger,
        connectors=connectors,
        action_count=_count_actions(actions),
        source=source,
        description=str(description)[:MAX_DESCRIPTION_CHARS] if description else None,
    )


def _api_from_ref(key: str, ref: Any) -> str | None:
    candidates = [key]
    if isinstance(ref, dict):
        api = ref.get("api")
        candidates += [
            str(ref.get("id", "")),
            str(ref.get("apiName", "")),
            str(api.get("name", "")) if isinstance(api, dict) else str(api or ""),
        ]
    return next((api for c in candidates if (api := _api_name(c))), None)


def _connector_label(api: str) -> str:
    short = api.lower().removeprefix("shared_")
    return CONNECTOR_NAMES.get(short, re.sub(r"[_\-]+", " ", short).strip().title())


def _host_api(node: dict, ref_api: dict[str, str]) -> str | None:
    inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
    host = inputs.get("host") if isinstance(inputs.get("host"), dict) else {}
    for value in (host.get("apiId"), host.get("apiName")):
        if isinstance(value, str) and (api := _api_name(value)):
            return api
    conn = host.get("connectionName") or host.get("connectionReferenceName")
    if not conn and isinstance(host.get("connection"), dict):
        conn = host["connection"].get("name") or host["connection"].get("referenceName")
    if isinstance(conn, str):
        m = re.search(r"\['([^']+)'\]", conn)  # @parameters('$connections')['shared_x']['connectionId']
        key = (m.group(1) if m else conn).lower()
        if key in ref_api:
            return ref_api[key]
        return _api_name(key)
    return None


def _node_connector(node: dict, ref_api: dict[str, str]) -> str | None:
    api = _host_api(node, ref_api)
    if api:
        return _connector_label(api)
    return BUILTIN_CONNECTORS.get(str(node.get("type", "")).lower())


def _describe_trigger(node: Any, ref_api: dict[str, str]) -> str:
    if not isinstance(node, dict):
        return "Unknown"
    kind = str(node.get("kind", "")).lower()
    ttype = str(node.get("type", ""))
    api = _host_api(node, ref_api)
    if api:
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        host = inputs.get("host") if isinstance(inputs.get("host"), dict) else {}
        op = host.get("operationId") or inputs.get("path") or ttype
        return f"{_connector_label(api)}: {op}"
    if ttype.lower() == "recurrence":
        rec = node.get("recurrence") or {}
        every = f" every {rec.get('interval', 1)} {rec.get('frequency', '')}".rstrip() if rec else ""
        return f"Recurrence (scheduled{every})"
    if ttype.lower() == "request":
        return {"button": "Manual (button)", "powerapp": "Power Apps", "powerappv2": "Power Apps",
                "http": "HTTP request", "skills": "Copilot Studio"}.get(kind, "HTTP request")
    return ttype or "Unknown"


def _children(node: dict) -> list[dict]:
    """Nested action containers: scopes, conditions (else), switches (cases/default), loops."""
    groups = [node.get("actions")]
    if isinstance(node.get("else"), dict):
        groups.append(node["else"].get("actions"))
    if isinstance(node.get("default"), dict):
        groups.append(node["default"].get("actions"))
    if isinstance(node.get("cases"), dict):
        groups += [c.get("actions") for c in node["cases"].values() if isinstance(c, dict)]
    return [g for g in groups if isinstance(g, dict)]


def _walk(actions: Any, depth: int = 0) -> list[dict]:
    if not isinstance(actions, dict) or depth > MAX_NESTING:
        return []
    out = []
    for node in actions.values():
        if isinstance(node, dict):
            out.append(node)
            for group in _children(node):
                out += _walk(group, depth + 1)
    return out


def _count_actions(actions: Any) -> int:
    return len(_walk(actions))


def _add(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


# ---------------------------------------------------------------- overlap detection

STOPWORDS = {
    "a", "an", "the", "and", "or", "to", "of", "for", "in", "on", "from", "with", "into", "by", "at", "as",
    "is", "are", "be", "it", "its", "this", "that", "when", "new", "flow", "flows", "step", "process",
    "automated", "automation", "auto", "item", "items", "all", "each", "any", "via", "using", "then", "copy",
    "v2", "v3", "if", "per", "our", "their", "them",
}

SYSTEM_ALIASES = {
    "outlook": {"outlook", "outlook.com", "mail", "email", "exchange", "office 365 outlook"},
    "microsoft teams": {"teams", "microsoft teams"},
    "sharepoint": {"sharepoint", "sharepoint online"},
    "dataverse": {"dataverse", "common data service", "dynamics 365"},
    "excel": {"excel", "excel online"},
}


def _stem(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    elif word.endswith("ing") and len(word) > 5:
        word = word[:-3]
    elif word.endswith("ed") and len(word) > 4:
        word = word[:-2]
    elif word.endswith("es") and len(word) > 4 and not word.endswith("ses"):
        word = word[:-2]
    elif word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        word = word[:-1]
    if word.endswith("e") and len(word) > 4:
        word = word[:-1]
    return word


def _tokens(*texts: str | None) -> set[str]:
    words = re.findall(r"[a-z0-9]+", " ".join(t for t in texts if t).lower())
    return {_stem(w) for w in words if w not in STOPWORDS and len(w) > 1}


def _weight(tokens: set[str], idf: dict[str, float]) -> float:
    return sum(idf.get(t, 1.0) for t in tokens)


def _dice(a: set[str], b: set[str], idf: dict[str, float]) -> float:
    total = _weight(a, idf) + _weight(b, idf)
    return 2 * _weight(a & b, idf) / total if total else 0.0


def _system_matches(system: str | None, connectors: list[str]) -> str | None:
    if not system:
        return None
    sys_l = system.lower()
    sys_names = {sys_l} | next((v for k, v in SYSTEM_ALIASES.items() if sys_l in v or sys_l == k), set())
    for c in connectors:
        c_l = c.lower()
        c_names = {c_l} | next((v for k, v in SYSTEM_ALIASES.items() if c_l in v or c_l == k), set())
        if sys_names & c_names or sys_l in c_l or c_l in sys_l:
            return c
    return None


def find_overlaps(profile: ProcessProfile, flows: list[FlowInfo], threshold: float = 0.5) -> list[Overlap]:
    """Steps that an existing flow may already automate (score >= threshold), best first.

    Terms are weighted by rarity across all steps and flows (IDF), so words every step shares
    ("claim") count less than distinctive ones ("eligibility"). Text similarity is the best of:
    name-to-name Dice, full-text Dice, and 0.9 x the share of the step name found in the flow
    (flow description terms count half). score = 0.75 * text + 0.25 if the step's system is one of
    the flow's connectors. A pair needs text >= 0.5 as well, so a shared connector alone never matches.
    """
    docs = [_tokens(s.name, s.description) for s in profile.steps] + [_tokens(f.name, f.description) for f in flows]
    df: dict[str, int] = {}
    for doc in docs:
        for t in doc:
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log(1 + len(docs) / n) for t, n in df.items()}

    found = []
    for step in profile.steps:
        step_name = _tokens(step.name)
        step_all = _tokens(step.name, step.description)
        for flow in flows:
            flow_name = _tokens(flow.name)
            flow_all = _tokens(flow.name, flow.description)
            shared = step_all & flow_all
            if not shared:
                continue
            in_name, in_desc = step_name & flow_name, (step_name & flow_all) - flow_name
            name_weight = _weight(step_name, idf)
            coverage = (_weight(in_name, idf) + 0.5 * _weight(in_desc, idf)) / name_weight if name_weight else 0.0
            text = max(_dice(step_name, flow_name, idf), _dice(step_all, flow_all, idf), 0.9 * coverage)
            connector = _system_matches(step.system, flow.connectors)
            score = round(min(1.0, 0.75 * text + (0.25 if connector else 0.0)), 2)
            if text < 0.5 or score < threshold:
                continue
            reason = f"shared terms: {', '.join(sorted(shared))}"
            if connector:
                reason += f"; flow uses {connector}, the step's system ({step.system})"
            found.append(Overlap(step=step.name, flow=flow.name, score=score, reason=reason))
    return sorted(found, key=lambda o: (-o.score, o.step, o.flow))
