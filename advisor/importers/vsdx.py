"""Import a Visio (.vsdx) flowchart as a profile update.

A .vsdx is a zip of XML parts: visio/pages/pageN.xml holds the shapes and the
<Connects> between them, visio/masters/masters.xml names the stencil masters
("Process", "Decision", "Start/End", "Swimlane"...). Connectors give the order
of the steps; swimlanes give actors (or systems). Nothing is ever extracted to
disk and zip entry names are only used as lookup keys.
"""

from __future__ import annotations

import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib

from advisor.importers import ImportResult
from advisor.importers.bpmn import Graph, Node, clean, graph_to_result, local, safe_xml_root

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ENTRIES = 2000
MAX_PART_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_PAGES = 50

_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


class _Budget:
    """Caps the total number of decompressed bytes read from the archive."""

    def __init__(self, zf: zipfile.ZipFile) -> None:
        self.zf = zf
        self.used = 0
        self.names = {info.filename for info in zf.infolist()}

    def read(self, name: str) -> bytes | None:
        if name not in self.names:
            return None
        try:
            with self.zf.open(name) as fh:
                data = fh.read(MAX_PART_BYTES + 1)  # don't trust the header's declared size
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error) as e:
            raise ValueError("The Visio file is damaged, encrypted or uses an unsupported compression.") from e
        if len(data) > MAX_PART_BYTES:
            raise ValueError(f"The Visio file part '{name}' is too large.")
        self.used += len(data)
        if self.used > MAX_TOTAL_BYTES:
            raise ValueError("The Visio file expands to too much data.")
        return data


def parse_vsdx(data: bytes) -> ImportResult:
    """Read a .vsdx flowchart into a ProfileUpdate (steps in connector order, systems, notes)."""
    if not data:
        raise ValueError("The Visio file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"The Visio file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError("Not a .vsdx file (it is not a zip archive).") from e
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ValueError("The Visio file has too many parts.")
        if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
            raise ValueError("The Visio file expands to too much data.")
        budget = _Budget(zf)
        pages = _page_parts(budget)
        if not pages:
            raise ValueError("No Visio pages found; is this a .vsdx drawing?")
        notes: list[str] = []
        if len(pages) > MAX_PAGES:
            notes.append(f"Only the first {MAX_PAGES} of {len(pages)} pages were read.")
            pages = pages[:MAX_PAGES]
        masters = _masters(budget)
        graphs = []
        for page_name, part in pages:
            raw = budget.read(part)
            if raw is None:
                notes.append(f"Page '{page_name}' is missing from the file and was skipped.")
                continue
            graph = _page_graph(safe_xml_root(raw, "Visio page", MAX_PART_BYTES), masters, page_name)
            if any(n.kind != "lane" for n in graph.nodes.values()):
                graphs.append(graph)
        title = _title(budget)

    result = graph_to_result(graphs, title=title, source="Visio diagram")
    result.notes[1:1] = notes
    return result


def _page_parts(budget: _Budget) -> list[tuple[str, str]]:
    """(page name, zip part) in the drawing's page order; foreground pages only."""
    pages: list[tuple[str, str]] = []
    index = budget.read("visio/pages/pages.xml")
    rels = budget.read("visio/pages/_rels/pages.xml.rels")
    if index and rels:
        targets = {
            el.get("Id"): el.get("Target", "")
            for el in safe_xml_root(rels, "Visio").iter()
            if local(el.tag) == "Relationship"
        }
        for page in safe_xml_root(index, "Visio").iter():
            if local(page.tag) != "Page" or page.get("Background") == "1":
                continue
            rel = next((c for c in page if local(c.tag) == "Rel"), None)
            rid = rel.get(f"{{{_REL_NS}}}id") or rel.get("id") if rel is not None else None
            target = targets.get(rid or "")
            if target:
                part = posixpath.normpath(posixpath.join("visio/pages", target))
                pages.append((clean(page.get("NameU") or page.get("Name")) or posixpath.basename(part), part))
    if not pages:  # no index: fall back to page1.xml, page2.xml...
        found = [n for n in budget.names if re.fullmatch(r"visio/pages/page(\d+)\.xml", n)]
        found.sort(key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
        pages = [(f"Page-{i + 1}", n) for i, n in enumerate(found)]
    return pages


def _masters(budget: _Budget) -> dict[str, str]:
    """Master ID -> lower-case universal name."""
    raw = budget.read("visio/masters/masters.xml")
    if not raw:
        return {}
    return {
        el.get("ID"): clean(el.get("NameU") or el.get("Name")).lower()
        for el in safe_xml_root(raw, "Visio").iter()
        if local(el.tag) == "Master" and el.get("ID")
    }


def _title(budget: _Budget) -> str | None:
    raw = budget.read("docProps/core.xml")
    if not raw:
        return None
    try:
        root = safe_xml_root(raw, "Visio")
    except ValueError:
        return None
    return next((clean(el.text) for el in root.iter() if local(el.tag) == "title" and clean(el.text)), None)


def _kind(master: str, text: str) -> str:
    """Node kind from the master name (flowchart, cross-functional and BPMN stencils)."""
    words = set(re.findall(r"[a-z]+", master))
    if words & {"connector", "line", "arrow", "flow"}:
        return "connector"
    if words & {"title", "text", "annotation", "legend", "callout", "note"}:
        return "ignore"
    if words & {"swimlane", "lane", "band", "container", "pool", "separator", "phase"}:
        return "lane"
    if words & {"decision", "gateway"}:
        return "xor"
    if "terminator" in words or {"start", "end"} <= words:
        return "terminator"
    if "start" in words:
        return "start"
    if "end" in words:
        return "end"
    if "event" in words:
        return "event"
    if words & {"subprocess", "predefined"} or "sub-process" in master:
        return "subprocess"
    if not master and re.fullmatch(r"(start|begin|end|finish|stop|done)", text.strip().lower()):
        return "terminator"  # master unknown: a bare Start/End label
    return "task" if text else "ignore"


def _cells(shape: ET.Element) -> dict[str, float]:
    out: dict[str, float] = {}
    for c in shape:
        if local(c.tag) == "Cell" and c.get("N") in ("PinX", "PinY", "Width", "Height", "BeginX", "EndX"):
            try:
                out[c.get("N")] = float(c.get("V", ""))
            except ValueError:
                pass
    return out


def _text(shape: ET.Element) -> str:
    """Shape text; for groups without own text (e.g. swimlanes) the first sub-shape text."""
    own = next((c for c in shape if local(c.tag) == "Text"), None)
    if own is not None and clean("".join(own.itertext())):
        return clean("".join(own.itertext()))
    for sub in shape.iter():
        if sub is not shape and local(sub.tag) == "Text" and clean("".join(sub.itertext())):
            return clean("".join(sub.itertext()))
    return ""


def _page_graph(root: ET.Element, masters: dict[str, str], page_name: str) -> Graph:
    graph = Graph(label=page_name)
    shapes_el = next((c for c in root if local(c.tag) == "Shapes"), None)
    lanes: list[tuple[str, float, float, float, float]] = []  # (text, x0, x1, y0, y1)
    connectors: set[str] = set()
    labels: dict[str, str] = {}
    pending: list[tuple[str, ET.Element, str, dict[str, float]]] = []
    connects_el = next((c for c in root if local(c.tag) == "Connects"), None)
    connects = [c for c in (list(connects_el) if connects_el is not None else []) if local(c.tag) == "Connect"]
    one_d = {c.get("FromSheet") for c in connects if c.get("FromCell") in ("BeginX", "EndX")}

    for shape in list(shapes_el) if shapes_el is not None else []:
        if local(shape.tag) != "Shape" or not shape.get("ID"):
            continue
        sid = shape.get("ID")
        text = _text(shape)
        cells = _cells(shape)
        kind = _kind(masters.get(shape.get("Master", ""), ""), text)
        if sid in one_d or ("BeginX" in cells and not shape.get("Master")):
            kind = "connector"  # glued by its ends, or a plain 1-D line drawn without a stencil
        if kind == "connector":
            connectors.add(sid)
            if text:
                labels[sid] = text
        elif kind == "lane":
            if text and {"PinX", "PinY", "Width", "Height"} <= cells.keys():
                w, h = cells["Width"] / 2, cells["Height"] / 2
                lanes.append((text, cells["PinX"] - w, cells["PinX"] + w, cells["PinY"] - h, cells["PinY"] + h))
        elif kind != "ignore":
            pending.append((sid, shape, kind, cells))

    # Smaller lanes first so a lane inside a pool/container wins.
    lanes.sort(key=lambda l: (l[2] - l[1]) * (l[4] - l[3]))
    for sid, shape, kind, cells in pending:
        lane = None
        if "PinX" in cells and "PinY" in cells:
            x, y = cells["PinX"], cells["PinY"]
            lane = next((t for t, x0, x1, y0, y1 in lanes if x0 <= x <= x1 and y0 <= y <= y1), None)
        graph.nodes[sid] = Node(id=sid, kind=kind, name=_text(shape), lane=lane, order=len(graph.nodes))

    # <Connect FromSheet=connector FromCell=BeginX|EndX ToSheet=shape/>
    ends: dict[str, dict[str, str]] = {}
    for c in connects:
        frm, to, cell = c.get("FromSheet"), c.get("ToSheet"), c.get("FromCell", "")
        if frm in connectors and to in graph.nodes and cell in ("BeginX", "EndX"):
            ends.setdefault(frm, {})[cell] = to
    for conn, e in ends.items():
        if "BeginX" in e and "EndX" in e and e["BeginX"] != e["EndX"]:
            graph.edges.append((e["BeginX"], e["EndX"], labels.get(conn, "")))

    # Free text boxes (no stencil, not connected) are labels, not steps.
    linked = {n for s, t, _ in graph.edges for n in (s, t)}
    for sid, shape, kind, _ in pending:
        if not shape.get("Master") and sid not in linked:
            del graph.nodes[sid]

    # Start/End terminators: decide by text, else by direction of the connectors.
    incoming = {t for _, t, _ in graph.edges}
    outgoing = {s for s, _, _ in graph.edges}
    for node in graph.nodes.values():
        if node.kind != "terminator":
            continue
        name = node.name.lower()
        if re.search(r"\b(end|stop|finish|done|complete)\b", name) or (node.id in incoming and node.id not in outgoing):
            node.kind = "end"
        else:
            node.kind = "start"
    return graph
