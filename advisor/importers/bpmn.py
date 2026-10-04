"""Import a BPMN 2.0 diagram (Camunda, Signavio, Bizagi, Visio export...) as a profile update.

The diagram gives the step list, order, actors, decision points and systems.
Timings, volumes and data sensitivity are never in a diagram, so they stay
unset for the interview (or the user) to fill in.

The flow-graph helpers here (`Node`, `Graph`, `graph_to_result`) are shared
with the Visio importer.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field

from advisor.importers import ImportResult
from advisor.models import InputType, ProfileUpdate, Step, System

MAX_BYTES = 10 * 1024 * 1024
MAX_DOC_CHARS = 500

# BPMN element local name -> node kind
_TASK_TAGS = {
    "task": "task",
    "userTask": "user",
    "manualTask": "manual",
    "serviceTask": "service",
    "scriptTask": "script",
    "sendTask": "send",
    "receiveTask": "receive",
    "businessRuleTask": "rule",
    "callActivity": "call",
    "subProcess": "subprocess",
    "adHocSubProcess": "subprocess",
    "transaction": "subprocess",
}
_EVENT_TAGS = {
    "startEvent": "start",
    "endEvent": "end",
    "intermediateCatchEvent": "event",
    "intermediateThrowEvent": "event",
    "boundaryEvent": "event",
}
_GATEWAY_TAGS = {"exclusiveGateway", "parallelGateway", "inclusiveGateway", "eventBasedGateway", "complexGateway"}
TASK_KINDS = set(_TASK_TAGS.values())

# Known applications, checked in order (more specific names first). Pattern -> canonical name.
KNOWN_SYSTEMS: list[tuple[str, str]] = [
    (r"sap\s*ariba|\bariba", "SAP Ariba"),
    (r"successfactors", "SAP SuccessFactors"),
    (r"\bsap\b|\bs/?4\s*hana\b", "SAP"),
    (r"\boutlook\b", "Outlook"),
    (r"\bexchange\b", "Exchange"),
    (r"\bgmail\b", "Gmail"),
    (r"\bexcel\b", "Excel"),
    (r"\bsharepoint\b", "SharePoint"),
    (r"\bonedrive\b", "OneDrive"),
    (r"\b(ms |microsoft )?teams\b", "Microsoft Teams"),
    (r"\bsalesforce\b", "Salesforce"),
    (r"\bdynamics\b", "Dynamics 365"),
    (r"\boracle\b", "Oracle"),
    (r"\bservicenow\b", "ServiceNow"),
    (r"\bworkday\b", "Workday"),
    (r"\bjira\b", "Jira"),
    (r"\bdocusign\b", "DocuSign"),
    (r"\bquickbooks\b", "QuickBooks"),
    (r"\bxero\b", "Xero"),
    (r"\bzendesk\b", "Zendesk"),
    (r"\bhubspot\b", "HubSpot"),
    (r"\bslack\b", "Slack"),
    (r"\bcoupa\b", "Coupa"),
    (r"\bepic\b", "Epic"),
    (r"\bcerner\b", "Cerner"),
    (r"\bgoogle sheets\b", "Google Sheets"),
]
_SYSTEMISH = re.compile(r"\b(system|application|app|portal|erp|crm|database|db|platform|mainframe|server)\b", re.I)
_APPROVAL = re.compile(r"\b(approv\w*|sign[- ]?off|authori[sz]\w*|endorse\w*)\b", re.I)
_EMAIL = re.compile(r"\b(e-?mail\w*|inbox|mailbox)\b", re.I)
_SCANNED = re.compile(r"\b(scan\w*|ocr|fax\w*|paper form)\b", re.I)
_VOICE = re.compile(r"\b(phone call|voicemail|call recording)\b", re.I)
_HUMAN_KINDS = {"task", "user", "manual"}


@dataclass
class Node:
    id: str
    kind: str  # a TASK_KINDS value, or start | end | xor | gateway | event
    name: str = ""
    doc: str = ""
    lane: str | None = None
    pool: str | None = None
    message: bool = False  # message start event / message-triggered
    order: int = 0  # document order, used to break ties


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[tuple[str, str, str]] = field(default_factory=list)  # (source, target, label)
    label: str = ""  # page or process name, used in notes


# ---------------------------------------------------------------- XML safety


def safe_xml_root(data: bytes, what: str = "XML", max_bytes: int = MAX_BYTES) -> ET.Element:
    """Parse untrusted XML; rejects DTD/entity declarations and oversize input (ValueError)."""
    if not data or not data.strip():
        raise ValueError(f"The {what} file is empty.")
    if len(data) > max_bytes:
        raise ValueError(f"The {what} file is larger than {max_bytes // (1024 * 1024)} MB.")
    probe = data.replace(b"\x00", b"").upper()  # also catches UTF-16 text
    if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
        raise ValueError(f"The {what} file contains a DOCTYPE/ENTITY declaration, which is not allowed.")
    try:
        return ET.fromstring(data)
    except ET.ParseError as e:
        if "unbound prefix" not in str(e):
            raise ValueError(f"Could not read the {what} file: {e}") from e
    # Some exporters use prefixes like bpmn: without declaring them; strip the prefixes and retry.
    text = _decode_xml(data)
    text = re.sub(r"<(/?)[A-Za-z_][\w.-]*:", r"<\1", text)
    text = re.sub(r"(\s)(?!xmlns\b)[A-Za-z_][\w.-]*:([A-Za-z_][\w.-]*\s*=)", r"\1\2", text)
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"Could not read the {what} file: {e}") from e


def _decode_xml(data: bytes) -> str:
    """Decode XML bytes using the declared encoding (else UTF-8, then cp1252) and drop the XML declaration."""
    head = data[:200].replace(b"\x00", b"")
    m = re.search(rb"<\?xml[^>]*encoding\s*=\s*[\"']([A-Za-z0-9._-]+)[\"']", head)
    candidates = ([m.group(1).decode("ascii")] if m else []) + ["utf-8-sig", "cp1252"]
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        candidates.insert(0, "utf-16")
    text = None
    for enc in candidates:
        try:
            text = data.decode(enc)
            break
        except (LookupError, UnicodeDecodeError):
            continue
    if text is None:
        text = data.decode("utf-8", errors="replace")
    # a str carrying an encoding declaration is rejected by ElementTree, and the text is already decoded
    return re.sub(r"^\s*<\?xml[^>]*\?>", "", text.lstrip("﻿"), count=1)


def local(tag: object) -> str:
    """Tag name without namespace or prefix."""
    if not isinstance(tag, str):
        return ""  # comments / processing instructions
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


# ---------------------------------------------------------------- BPMN parsing


def parse_bpmn(data: bytes) -> ImportResult:
    """Read a BPMN 2.0 XML file into a ProfileUpdate (steps in flow order, systems, notes)."""
    root = safe_xml_root(data, "BPMN")
    if not any(local(el.tag) in _TASK_TAGS or local(el.tag) in _EVENT_TAGS for el in root.iter()):
        raise ValueError("No BPMN tasks or events found; is this a BPMN 2.0 file?")

    # Pools: process id -> participant name
    pools: dict[str, str] = {}
    for el in root.iter():
        if local(el.tag) == "participant" and el.get("processRef"):
            pools[el.get("processRef")] = clean(el.get("name"))

    graph = Graph()
    processes = [el for el in root.iter() if local(el.tag) == "process"] or [root]
    process_names: list[str] = []
    for proc in processes:
        pool = pools.get(proc.get("id", "")) or None
        # The process name beats the pool (often a role), unless the pool name is the fuller form of it.
        proc_name = clean(proc.get("name"))
        process_names.append(pool if pool and (not proc_name or proc_name.lower() in pool.lower()) else proc_name or pool)
        lanes = _lane_map(proc)
        _collect(proc, graph, pool, lanes)

    # Message flows connect pools; treat them as ordering edges.
    for el in root.iter():
        if local(el.tag) == "messageFlow":
            src, tgt = el.get("sourceRef"), el.get("targetRef")
            if src in graph.nodes and tgt in graph.nodes:
                graph.edges.append((src, tgt, clean(el.get("name"))))

    defs_name = clean(root.get("name"))
    # Prefer a business pool name over a system pool (e.g. "Sales" rather than "SAP").
    names = [n for n in process_names if n]
    title = next((n for n in names if not _container_system(n)), None) or next(iter(names), None) or defs_name or None
    notes = []
    named_pools = sorted({p for p in pools.values() if p})
    if len(named_pools) > 1:
        notes.append(f"Diagram has {len(named_pools)} pools: {', '.join(named_pools)}.")
    result = graph_to_result([graph], title=title, source="BPMN diagram")
    result.notes[:0] = notes
    return result


def _lane_map(proc: ET.Element) -> dict[str, str]:
    """Node id -> lane name (nested lanes: the innermost wins, as they come later in iteration)."""
    lanes: dict[str, str] = {}
    for lane in proc.iter():
        if local(lane.tag) != "lane":
            continue
        name = clean(lane.get("name"))
        for ref in lane:
            if local(ref.tag) == "flowNodeRef" and ref.text and name:
                lanes[ref.text.strip()] = name
    return lanes


def _collect(container: ET.Element, graph: Graph, pool: str | None, lanes: dict[str, str]) -> None:
    """Add flow nodes and sequence flows of one process; sub-processes count as a single step."""
    attached: list[tuple[str, str]] = []
    stack = [iter(container)]
    while stack:
        el = next(stack[-1], None)
        if el is None:
            stack.pop()
            continue
        tag = local(el.tag)
        node_id = el.get("id")
        if tag in ("BPMNDiagram", "extensionElements", "laneSet"):
            continue
        if tag == "sequenceFlow":
            src, tgt = el.get("sourceRef"), el.get("targetRef")
            if src and tgt:
                graph.edges.append((src, tgt, clean(el.get("name"))))
            continue
        kind = _TASK_TAGS.get(tag) or _EVENT_TAGS.get(tag)
        if tag in _GATEWAY_TAGS:
            kind = "xor" if tag == "exclusiveGateway" else "gateway"
        if kind and node_id:
            has_message = any(local(c.tag) == "messageEventDefinition" for c in el)
            graph.nodes[node_id] = Node(
                id=node_id,
                kind=kind,
                name=clean(el.get("name")),
                doc=_documentation(el),
                lane=lanes.get(node_id),
                pool=pool,
                message=has_message or tag == "receiveTask",
                order=len(graph.nodes),
            )
            if tag == "boundaryEvent" and el.get("attachedToRef"):
                attached.append((el.get("attachedToRef"), node_id))
            if kind == "subprocess":
                continue  # don't descend: the sub-process is one step
        stack.append(iter(el))
    graph.edges += [(task, event, "") for task, event in attached]


def _documentation(el: ET.Element) -> str:
    parts = [clean("".join(c.itertext())) for c in el if local(c.tag) == "documentation"]
    text = " ".join(p for p in parts if p)
    return text[:MAX_DOC_CHARS]


# ---------------------------------------------------------------- shared graph -> steps


def order_nodes(graph: Graph) -> tuple[list[str], list[str], int]:
    """Walk the flow from its start nodes.

    Returns (reachable node ids in flow order, unreachable node ids in document order,
    number of loop-back edges ignored for ordering).
    """
    nodes = graph.nodes
    succ: dict[str, list[str]] = {n: [] for n in nodes}
    for src, tgt, _ in graph.edges:
        if src in nodes and tgt in nodes and tgt not in succ[src]:
            succ[src].append(tgt)
    has_incoming = {t for s in succ.values() for t in s}
    by_order = sorted(nodes.values(), key=lambda n: n.order)
    starts = [n.id for n in by_order if n.kind == "start"]
    # Flows drawn without a start event begin at a node with outgoing but no incoming flows.
    starts += [n.id for n in by_order if n.kind != "start" and succ[n.id] and n.id not in has_incoming]
    if not starts and by_order:
        starts = [by_order[0].id]

    # DFS from the starts to find reachable nodes and back edges (loops).
    state: dict[str, int] = {}  # 1 = on stack, 2 = done
    back: set[tuple[str, str]] = set()
    for s in starts:
        if s in state:
            continue
        state[s] = 1
        stack = [(s, iter(succ[s]))]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[node] = 2
                stack.pop()
            elif state.get(nxt) == 1:
                back.add((node, nxt))
            elif nxt not in state:
                state[nxt] = 1
                stack.append((nxt, iter(succ[nxt])))

    # Kahn's algorithm (FIFO, so parallel branches come out breadth-first) without back edges.
    indeg = {n: 0 for n in state}
    for src in state:
        for tgt in succ[src]:
            if (src, tgt) not in back:
                indeg[tgt] += 1
    queue = deque(s for s in starts if s in state and indeg[s] == 0)
    seen_q = set(queue)
    ordered: list[str] = []
    while queue:
        node = queue.popleft()
        ordered.append(node)
        for tgt in succ[node]:
            if (node, tgt) in back:
                continue
            indeg[tgt] -= 1
            if indeg[tgt] == 0 and tgt not in seen_q:
                seen_q.add(tgt)
                queue.append(tgt)
    # Safety net: anything reachable but not emitted (should not happen) keeps DFS discovery order.
    ordered += [n for n in state if n not in seen_q]
    unreachable = [n.id for n in by_order if n.id not in state]
    return ordered, unreachable, len(back)


def match_system(text: str | None) -> str | None:
    """Canonical name of a well-known application mentioned in the text."""
    for pattern, name in KNOWN_SYSTEMS:
        if text and re.search(pattern, text, re.I):
            return name
    return None


def _container_system(name: str | None) -> str | None:
    """Lane/pool/page name used as a system when it is one (e.g. 'SAP', 'Claims System')."""
    if not name:
        return None
    return match_system(name) or (name if _SYSTEMISH.search(name) else None)


def graph_to_result(graphs: list[Graph], *, title: str | None, source: str) -> ImportResult:
    """Turn ordered flow graphs into steps, systems and notes (no timings are invented)."""
    steps: list[Step] = []
    systems: list[str] = []
    notes: list[str] = []
    used: dict[str, int] = {}
    unnamed = 0

    for graph in graphs:
        ordered, unreachable, loops = order_nodes(graph)
        nodes = graph.nodes
        preds: dict[str, list[tuple[str, str]]] = {n: [] for n in nodes}
        for src, tgt, label in graph.edges:
            if src in nodes and tgt in nodes:
                preds[tgt].append((src, label))
        email_fed = _email_fed_tasks(graph)
        where = f" on '{graph.label}'" if graph.label and len(graphs) > 1 else ""
        if loops:
            notes.append(f"{loops} loop-back flow(s){where} (rework) were ignored when ordering steps.")
        unreachable_tasks = [n for n in unreachable if nodes[n].kind in TASK_KINDS]
        if unreachable_tasks:
            names = ", ".join(f"'{nodes[n].name or n}'" for n in unreachable_tasks)
            notes.append(f"Not connected to the start of the flow{where}, added at the end: {names}.")

        for node_id in ordered + unreachable_tasks:
            node = nodes[node_id]
            if node.kind not in TASK_KINDS:
                continue
            name = node.name
            if not name:
                unnamed += 1
                name = f"Unnamed step {unnamed}"
                notes.append(f"A {node.kind} task without a name was called '{name}'.")
            key = name.lower()
            used[key] = used.get(key, 0) + 1
            if used[key] > 1:
                new = f"{name} ({used[key]})"
                notes.append(f"Step name '{name}' appears more than once; the repeat is called '{new}'.")
                name = new
                used[new.lower()] = 1
            step, step_notes = _make_step(node, name, preds[node_id], nodes, node_id in email_fed)
            steps.append(step)
            notes += step_notes
            if step.system and step.system.lower() not in {s.lower() for s in systems}:
                systems.append(step.system)

    if len(graphs) > 1:
        pages = ", ".join(f"'{g.label}'" for g in graphs if g.label)
        notes.insert(0, f"{len(graphs)} pages read{': ' + pages if pages else ''}; their steps are listed page by page.")
    notes.insert(0, f"Read {len(steps)} step(s) and {len(systems)} system(s) from the {source}.")
    if steps:
        notes.append(
            "Diagrams do not hold handling times, volumes, costs or data sensitivity; "
            "please add these before running the analysis."
        )
    update = ProfileUpdate(
        name=title or None,
        steps=steps or None,
        systems=[System(name=s) for s in systems] or None,
    )
    return ImportResult(update=update, notes=notes)


def _email_fed_tasks(graph: Graph) -> set[str]:
    """Tasks directly triggered (through gateways/events only) by a message start mentioning email."""
    succ: dict[str, list[str]] = {}
    for src, tgt, _ in graph.edges:
        succ.setdefault(src, []).append(tgt)
    fed: set[str] = set()
    for node in graph.nodes.values():
        if node.kind != "start" or not _EMAIL.search(f"{node.name} {node.doc}"):
            continue
        queue, seen = deque(succ.get(node.id, [])), set()
        while queue:
            nid = queue.popleft()
            if nid in seen or nid not in graph.nodes:
                continue
            seen.add(nid)
            if graph.nodes[nid].kind in TASK_KINDS:
                fed.add(nid)
            else:
                queue.extend(succ.get(nid, []))
    return fed


def _make_step(
    node: Node, name: str, preds: list[tuple[str, str]], nodes: dict[str, Node], email_fed: bool
) -> tuple[Step, list[str]]:
    text = f"{node.name} {node.doc}"
    notes: list[str] = []
    fields: dict = {}

    if node.kind == "rule":
        fields.update(input_type="structured", rules_documented=True, requires_judgment=False)
    elif node.kind in ("service", "script"):
        fields.update(input_type="structured", requires_judgment=False, is_approval=False)
    if node.kind in _HUMAN_KINDS and _APPROVAL.search(node.name):
        fields["is_approval"] = True
        notes.append(f"'{name}' looks like an approval; it stays with a person.")

    input_type: InputType | None = fields.get("input_type")
    if input_type is None:
        if (node.kind == "receive" or email_fed or node.message) and _EMAIL.search(text):
            input_type = "email"
        elif email_fed:
            input_type = "email"
        elif _SCANNED.search(text):
            input_type = "scanned_document"
        elif _EMAIL.search(node.name) and node.kind != "send":
            input_type = "email"
        elif _VOICE.search(text):
            input_type = "voice"
    if input_type:
        fields["input_type"] = input_type

    system = (
        match_system(node.name)
        or match_system(node.doc)
        or _container_system(node.lane)
        or _container_system(node.pool)
    )
    if system:
        fields["system"] = system

    description: list[str] = [node.doc] if node.doc else []
    # Actor from the lane; the pool only counts when the step has no lane.
    actor = node.lane if node.lane else node.pool
    actor = None if not actor or _container_system(actor) else actor
    if actor:
        description.append(f"Actor: {actor}.")
        notes.append(f"'{name}' is done by {actor}.")
    for pred_id, label in preds:
        pred = nodes[pred_id]
        if pred.kind != "xor":
            continue
        decision = pred.name or "unnamed gateway"
        branch = f" (branch '{label}')" if label else ""
        description.append(f"Follows decision '{decision}'{branch}.")
        notes.append(f"Decision point before '{name}': '{decision}'{branch}.")
    if description:
        fields["description"] = " ".join(description)[: MAX_DOC_CHARS + 200]

    return Step(name=name, **fields), notes
