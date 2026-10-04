"""Licence inventory from a CSV exported from the Microsoft 365 or Power Platform admin center.

Only spare (unassigned) licences matter: they can be reused for the new automation
instead of being bought, which `analysis.cost.compare(..., inventory=...)` offsets.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Literal

Category = Literal["premium", "process", "ai_builder", "other"]

# One AI Builder capacity add-on unit gives 1,000,000 service credits per month.
AI_BUILDER_CREDITS_PER_UNIT = 1_000_000
MAX_CSV_BYTES = 5 * 1024 * 1024

# Header aliases, compared after lower-casing and stripping non-alphanumerics.
SKU_HEADERS = ("productname", "product", "skuname", "licensename", "license", "licence", "licencename",
               "skupartnumber", "sku", "displayname", "subscription", "plan", "name", "capacitytype", "type")
TOTAL_HEADERS = ("totallicenses", "totallicences", "total", "purchased", "purchasedquantity", "prepaidunits",
                 "prepaidunitsenabled", "enabledunits", "enabled", "quantity", "totalunits", "units", "entitled")
ASSIGNED_HEADERS = ("assignedlicenses", "assignedlicences", "assigned", "consumedunits", "consumed", "assignedunits",
                    "used", "inuse")
AVAILABLE_HEADERS = ("availablelicenses", "availablelicences", "available", "unassigned", "availableunits",
                     "remaining")


@dataclass
class LicenseRow:
    sku: str
    category: Category
    total: float | None
    assigned: float | None
    available: float  # spare units; credits for AI Builder rows


@dataclass
class LicenseInventory:
    premium_available: int = 0  # Power Automate Premium / per user seats not assigned
    process_available: int = 0  # Process / per bot / unattended RPA licences not assigned
    ai_builder_credits_available: int = 0  # AI Builder credits per month not allocated
    rows: list[LicenseRow] = field(default_factory=list)


def classify_sku(sku: str) -> Category:
    """Map an admin-center product name or SKU part number to a licence category."""
    s = sku.lower().replace("_", " ")
    if "ai builder" in s or "aibuilder" in s or "cdsaicapacity" in s.replace(" ", "") or "ai capacity" in s:
        return "ai_builder"
    if "flow" not in s and "power automate" not in s and "powerautomate" not in s:
        return "other"
    if re.search(r"trial|viral|developer", s):
        return "other"  # trial, viral and developer seats are temporary: never spare capacity to reuse
    if re.search(r"free|office 365|microsoft 365|seeded", s) and "premium" not in s:
        return "other"
    # Check "unattended" before "attended": the latter is a substring.
    if re.search(r"process|per bot|unattended|hosted rpa|per flow|business process", s):
        return "process"
    if re.search(r"premium|per user|attended rpa|perusr|flow p2|flow p1", s):
        return "premium"
    return "other"


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


def _find(headers: list[str], aliases: tuple[str, ...], taken: set[int] = frozenset()) -> int | None:
    normed = [_norm(h) for h in headers]
    for alias in aliases:  # exact match first, in alias priority order
        for i, h in enumerate(normed):
            if h == alias and i not in taken:
                return i
    for alias in aliases:
        for i, h in enumerate(normed):
            if i not in taken and (h.startswith(alias) or h.endswith(alias)):
                return i
    return None


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip().replace(",", "").replace(" ", "").replace(" ", "")
    if not text or text in {"-", "n/a", "N/A"}:
        return None
    m = re.match(r"^-?\d+(\.\d+)?", text)
    return float(m.group(0)) if m else None


def _decode(data: bytes | str) -> str:
    if isinstance(data, str):
        return data
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    sample = data[:200]
    if len(sample) >= 4 and sample.count(b"\x00") >= len(sample) // 3:  # BOM-less UTF-16 (mostly NUL bytes)
        even = sample[0::2].count(b"\x00")
        return data.decode("utf-16-be" if even > len(sample) // 4 else "utf-16-le", errors="replace")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def load_license_csv(data: bytes | str) -> tuple[LicenseInventory, list[str]]:
    """Parse a licence export into spare-licence counts plus notes for the user."""
    notes: list[str] = []
    inventory = LicenseInventory()
    if len(data) > MAX_CSV_BYTES:
        return inventory, [f"Licence file skipped: larger than {MAX_CSV_BYTES // (1024 * 1024)} MB."]
    text = _decode(data).lstrip("﻿")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return inventory, ["Licence file is empty."]
    try:
        dialect = csv.Sniffer().sniff(lines[0], delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    csv.field_size_limit(max(csv.field_size_limit(), MAX_CSV_BYTES))  # fields up to the file cap are fine
    try:
        rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
    except csv.Error as e:
        return inventory, [f"Licence file could not be read as CSV ({e})."]
    headers = rows[0]
    taken: set[int] = set()
    found = {}
    for role, aliases in (("avail", AVAILABLE_HEADERS), ("assigned", ASSIGNED_HEADERS), ("total", TOTAL_HEADERS),
                          ("sku", SKU_HEADERS)):
        found[role] = _find(headers, aliases, taken)
        if found[role] is not None:
            taken.add(found[role])
    sku_i, total_i, assigned_i, avail_i = found["sku"], found["total"], found["assigned"], found["avail"]
    if sku_i is None or (avail_i is None and total_i is None):
        notes.append(
            "Licence file not recognised: need a product/SKU column and either an 'available' column "
            f"or 'total' and 'assigned' columns (found: {', '.join(headers)})."
        )
        return inventory, notes

    totals = {"premium": 0.0, "process": 0.0, "ai_builder": 0.0}
    for raw in rows[1:]:
        cell = lambda i: raw[i] if i is not None and i < len(raw) else None  # noqa: E731
        sku = (cell(sku_i) or "").strip()
        if not sku:
            continue
        category = classify_sku(sku)
        total, assigned, available = _number(cell(total_i)), _number(cell(assigned_i)), _number(cell(avail_i))
        if available is None:
            available = (total or 0) - (assigned or 0) if total is not None else 0.0
        available = max(0.0, available)
        if category == "ai_builder" and "credit" not in sku.lower() and 0 < available < 1000:
            notes.append(f"'{sku}': {available:g} capacity unit(s) counted as "
                         f"{available * AI_BUILDER_CREDITS_PER_UNIT:,.0f} credits/month.")
            available *= AI_BUILDER_CREDITS_PER_UNIT
        inventory.rows.append(LicenseRow(sku=sku, category=category, total=total, assigned=assigned, available=available))
        if category in totals:
            totals[category] += available

    inventory.premium_available = int(totals["premium"])
    inventory.process_available = int(totals["process"])
    inventory.ai_builder_credits_available = int(totals["ai_builder"])
    relevant = [r for r in inventory.rows if r.category != "other"]
    if not relevant:
        notes.append("No Power Automate or AI Builder licences found in the file.")
    else:
        notes.append(
            f"Spare licences: {inventory.premium_available} Power Automate Premium, "
            f"{inventory.process_available} Process (bot), "
            f"{inventory.ai_builder_credits_available:,} AI Builder credits/month."
        )
    ignored = len(inventory.rows) - len(relevant)
    if ignored:
        notes.append(f"{ignored} other licence row(s) ignored (not Power Automate or AI Builder).")
    return inventory, notes
