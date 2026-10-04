"""Heuristic (no-LLM) parser for a pasted SOP or a free-text process description.

It pulls out whatever it can recognise — steps, minutes, systems, input types,
volume, hourly cost, sensitive data — and explains every inference in `notes`.
Anything it is unsure about is left as None so the interviewer asks for it.
"""

from __future__ import annotations

import math
import re

from advisor.importers import ImportResult
from advisor.models import DataProfile, InputType, ProfileUpdate, Step, System

WORKING_DAYS_PER_MONTH = 22
WEEKS_PER_MONTH = 4.33

# Canonical name -> (regex, has_api_or_connector, is_legacy_desktop)
KNOWN_SYSTEMS: dict[str, tuple[str, bool | None, bool | None]] = {
    "Outlook": (r"outlook", True, False),
    "Excel": (r"excel", True, False),
    "SharePoint": (r"share\s?point", True, False),
    "Microsoft Teams": (r"(?:microsoft\s+|ms\s+)teams|(?-i:Teams)", True, False),
    "Dynamics 365": (r"dynamics\s*365|(?-i:Dynamics)", True, False),
    "Salesforce": (r"salesforce", True, False),
    "ServiceNow": (r"service\s?now", True, False),
    "SAP": (r"sap", True, None),
    "Oracle": (r"oracle", True, None),
    "Gmail": (r"gmail", True, False),
    "Word": (r"ms\s+word|microsoft\s+word", True, False),
    "Mainframe": (r"mainframe|green[\s-]screen|as/?400", False, True),
}
PERSON_OR_ROLE_RE = (
    r"\b(?:manager|director|supervisor|officer|clerk|customer|client|member|patient|lead|head|analyst|executive|agent|"
    r"administrator|admin|approver|requester|requestor|employee|staff|vendor|supplier|ceo|cfo|coo|cto|vp|controller|"
    r"accountant|assistant|auditor|associate|specialist|owner|corp|corporation|inc|ltd|llc|gmbh|co|company|group|"
    r"mr|mrs|ms|dr)\b\.?"
)
FILLER_RE = re.compile(r"^(?:(?:so|basically|well|ok|okay|and|then|also|like|actually|first|next|usually|normally)\b[,\s]*)+", re.I)
PREPOSITIONS = {"from", "by", "into", "in", "on", "at", "of", "with", "using", "via", "for", "through", "onto", "within"}
SYSTEM_NOUNS = r"(?:system|portal|app|application|tool|database|db|crm|erp|platform|website|site)"
NOT_SYSTEMS = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february",
    "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
    "the", "a", "an", "our", "their", "i", "we", "uae", "aed", "usd", "eur", "gbp", "sar", "inr", "dubai",
    "abu", "sharjah", "order", "addition", "case", "total", "general", "practice", "parallel", "batch",
    "batches", "time", "turn", "person", "pdf", "email", "english", "arabic", "step", "summary", "this",
    "that", "it", "they", "he", "she", "each", "every", "some", "all", "line", "bulk",
}

INPUT_KEYWORDS: list[tuple[InputType, str]] = [
    ("scanned_document", r"scan(?:ned|s)?|pdfs?|fax(?:ed|es)?|handwritten|paper forms?|photocop\w*|images? of"),
    ("voice", r"phone calls?|calls? (?:from|with|in)|voicemails?|voice|by phone|over the phone|on the phone|calls?\b"),
    ("email", r"e-?mails?(?! address)|inbox|mailbox"),
    ("unstructured", r"free[\s-]text|notes|letters?|narratives?|comments|unstructured|chat messages?"),
    ("semi_structured", r"semi[\s-]structured|invoices?|templates?|receipts?"),
    ("structured", r"forms?|spreadsheets?|csv|databases?|system fields?|structured|records?|tables?|drop-?downs?|excel"),
]
INPUT_SYNONYMS = {k: v for k, v in INPUT_KEYWORDS}

APPROVAL_RE = r"approv\w*|sign(?:s|ed)?[\s-]?off\b|authori[sz]\w*"
JUDGMENT_RE = r"decid\w*|assess\w*|review\w*|judge?\w*|judg(?:e)?ment|evaluat\w*|discretion|check (?:if|whether) (?:it is |it's )?appropriate"
LANGUAGE_RE = r"read\w*|classif\w*|categori[sz]\w*|summari[sz]\w*|interpret\w*|understand\w*|triag\w*|draft\w*|write\w*"
RULES_YES_RE = r"documented|checklist|sop\b|written rules|rule[\s-]based|per (?:the )?policy|according to (?:the )?rules"
RULES_NO_RE = r"not documented|undocumented|no written rules|no documented|in (?:their|people's|our) heads?|tribal knowledge|unwritten"
NEGATION_RE = r"\b(?:no|not|non|without|never|none|n't|nor|neither|zero)\b"

PHI_RE = r"patients?|diagnos[ie]s|medical|health\w*|clinical|prescriptions?|treatments?|icd[\s-]?10|phi\b|lab results?"
PII_RE = (
    r"names?|full name|emirates id|passports?|phone numbers?|mobile numbers?|addresses|address|email address(?:es)?"
    r"|date of birth|dob\b|national id|iban|bank accounts?|pii\b|personal data|personal information"
)
UAE_RE = r"\buae\b|united arab emirates|dubai|abu dhabi|sharjah|ajman|emirates id|\baed\b|dirhams?|\bdha\b|\bdoh\b"
RESIDENCY_YES_RE = r"(?:stay|stored?|kept|hosted|remain)\s+(?:with)?in\s+(?:the\s+)?uae|data residency|in-country|onshore|local hosting"
RESIDENCY_NO_RE = r"no (?:data )?residency|can (?:be stored|leave|go) (?:outside|abroad)|no restriction on (?:where|hosting)"

# Other ISO codes recognised next to a number (rates live in pricing_assumptions.json / live FX).
EXTRA_CURRENCY_CODES = ("QAR", "KWD", "BHD", "OMR", "JOD", "EGP", "CAD", "AUD", "CHF", "SGD", "JPY", "CNY", "ZAR", "PKR", "NZD")
CURRENCY_SYMBOLS = {
    "$": "USD", "usd": "USD", "dollar": "USD", "dollars": "USD", "us$": "USD",
    "aed": "AED", "dhs": "AED", "dh": "AED", "dirham": "AED", "dirhams": "AED",
    "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "£": "GBP", "gbp": "GBP", "pound": "GBP", "pounds": "GBP",
    "sar": "SAR", "riyal": "SAR", "riyals": "SAR",
    **{c.lower(): c for c in EXTRA_CURRENCY_CODES},
    "₹": "INR", "inr": "INR", "rs": "INR", "rupee": "INR", "rupees": "INR",
}
_CUR = r"(?:us\$|\$|€|£|₹|(?<![a-z])(?:usd|aed|dhs?|dirhams?|eur|euros?|gbp|pounds?|sar|riyals?|inr|rs\.?|rupees?|dollars?|qar|kwd|bhd|omr|jod|egp|cad|aud|chf|sgd|jpy|cny|zar|pkr|nzd)(?![a-z]))"
# Bounded and anchored so the scan cannot restart inside a long digit run (no quadratic backtracking).
# The lookbehinds stop the scan restarting inside scientific notation ("1e999" must not read as 999).
_NUM = (
    r"(?<![\d,.])(?<!\d[eE])(?<!\d[eE][+-])(?:\d{1,3}(?:,\d{3})+|\d{1,12})(?!\d)(?:\.\d{1,6})?"
    r"(?:[eE][+-]?\d{1,2}(?!\d))?(?:\s*(?:thousand|million|billion|mn|bn|k|m)\b)?"
)
MAX_TEXT_CHARS = 20_000  # longer pastes are truncated (with a note) so regex scans stay fast

STABILITY_PATTERNS = [
    ("changes_monthly", r"changes? (?:every|each) month|monthly changes?|changes? monthly|changes? (?:all the time|constantly|often|frequently|weekly)"),
    ("changes_quarterly", r"quarterly|every quarter|few times a year|changes? (?:occasionally|sometimes|every few months)"),
    ("stable", r"rarely changes?|changes? rarely|seldom changes?|changes? seldom|hardly (?:ever )?changes?|never changes?|(?:very |quite |fairly )?stable|hasn't changed|has not changed|same for years"),
]

LIST_ITEM_RE = re.compile(r"^\s*(?:(?:step\s*)?\d{1,2}\s*[.):\-]|step\s*\d{1,2}\b\s*[.):\-]?|[-*•–·>]|[a-z][.)])\s+(.+)$", re.I)
SEQUENCE_SPLIT_RE = re.compile(
    r"(?:[.;!?\n]+|,?\s*\band then\b|,?\s*\bthen\b|,\s*\bnext\b|^\s*next\b|\bafter that\b|\bafterwards\b|\bfinally\b|\bfirst(?:ly)?\b|\bsecond(?:ly)?\b|\blastly\b)",
    re.I,
)
SUBJECT_RE = re.compile(
    r"^(?:,|\s)*(?:and\s+)?(?:we|they|i|you|he|she|someone|somebody|staff|the staff|the team|our team)\s+",
    re.I,
)


# ---------- small public helpers (also used by the guided interviewer) ----------

def parse_number(text: str) -> float | None:
    """First number in `text`: '4k' -> 4000, '1,200' -> 1200, 'about 30' -> 30, 'a dozen' -> 12."""
    t = text.lower()
    m = re.search(
        r"(?<![\d,.])(?<!\d[eE])(?<!\d[eE][+-])(\d[\d,]{0,24}(?:\.\d+)?(?:e[+-]?\d{1,2}(?!\d))?|\.\d+)"
        r"(?:\s*(thousand|million|billion|mn|bn|k|m)\b)?",
        t,
    )
    if m:
        try:
            value = float(m.group(1).replace(",", ""))
        except ValueError:
            return None
        unit = m.group(2) or ""
        if unit in ("k", "thousand"):
            value *= 1000
        elif unit in ("m", "mn", "million"):
            value *= 1_000_000
        elif unit in ("bn", "billion"):
            value *= 1_000_000_000
        return value if math.isfinite(value) and value <= 1e15 else None
    words = {"half": 0.5, "one": 1, "a couple": 2, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "a dozen": 12, "twelve": 12, "fifteen": 15,
             "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "a hundred": 100, "hundred": 100, "none": 0,
             "zero": 0}
    for word, value in words.items():
        if re.search(rf"\b{word}\b", t):
            return float(value)
    return None


_DURATION_RE = re.compile(
    r"(?<![\d.,])(\d{1,6}(?:\.\d+)?)(?:\s*(?:-|–|to)\s*(\d{1,6}(?:\.\d+)?))?\s*(minutes?|mins?|m\b|hours?|hrs?|h\b|seconds?|secs?|s\b)"
    r"(?:\s*(?:and\s+)?(\d{1,3})\s*(?:minutes?|mins?|m\b))?"
)
# A target ("within 24h", "SLA of 2 hours") is a turnaround time, not handling time.
_SLA_BEFORE_RE = re.compile(
    r"(?:within|in under|under|sla(?: of)?|by|turnaround(?: of| time of| time)?|no later than|at most|deadline(?: of)?|before|inside)\s*(?:of\s+|about\s+|around\s+)?$"
)


def parse_minutes(text: str) -> float | None:
    """Handling time in minutes: '5 min', '1 hour 30 minutes', '2-3 min', 'half an hour', '90 sec'.

    Ignores turnaround targets ('within 24h') and prefers an explicit '(N min)'."""
    t = text.lower()
    if re.search(r"half (?:an|a) hour", t):
        return 30.0
    if re.search(r"(?:quarter of an|a quarter) hour", t):
        return 15.0
    candidates: list[tuple[float, bool]] = []  # (minutes, in parentheses)
    for m in _DURATION_RE.finditer(t):
        if _SLA_BEFORE_RE.search(t[max(0, m.start() - 24):m.start()]):
            continue
        low = float(m.group(1))
        value = (low + float(m.group(2))) / 2 if m.group(2) else low
        unit = m.group(3)
        if unit.startswith("h"):
            value *= 60
            if m.group(4):
                value += float(m.group(4))
        elif unit.startswith("s"):
            value /= 60
        in_parens = bool(re.search(r"\(\s*(?:~|about|approx\.?|around)?\s*$", t[max(0, m.start() - 12):m.start()]))
        candidates.append((round(value, 2), in_parens))
    if candidates:
        return next((v for v, p in candidates if p), candidates[0][0])
    if re.search(r"\ban hour\b|\bone hour\b", t):
        # "$30 an hour" / "30 bucks an hour" is a pay rate, not a handling time.
        pre = t.split("an hour")[0][-30:] if "an hour" in t else ""
        rate = re.search(r"\d|\bpa(?:y|id)\b|\bearn|\bbucks?\b|\bwage|\bsalary|\brate\b|\bcost|" + _CUR, pre)
        if not re.search(r"(?:per|an|a|/)\s*hour", pre[-12:]) and not rate:
            return 60.0
    if re.search(r"\ba (?:minute|min)\b", t):
        return 1.0
    if re.search(r"\b(?:a )?(?:few|couple of) minutes\b", t):
        return 3.0
    return None


_NOT_WORK_ITEMS = r"\b(?:days?|hours?|hrs?|shifts?|weeks?|months?|years?|people|persons?|staff|employees|agents?|clerks?|fte|ftes|users?|analysts?|operators?|members)\b"


def _g(n: float) -> str:
    """Plain number text without exponent: 2000000 -> '2000000', 0.5 -> '0.5'."""
    return f"{n:.12g}" if abs(n) < 1e12 else f"{n:.0f}"


_VOL_UNIT = r"(?:working day|business day|day|week|month|year|annum)"
_VOL_ADVERB = r"(?:daily|weekly|monthly|yearly|annually)"
_SEP = r"(?:(?<![a-z])(?:per|a|an|each|every)\s+|\s*/\s*)"
_VOLUME_PATTERNS = [
    # "4000 claims per month", "1,200 invoices/month", "200 a day"
    rf"(?P<n>{_NUM})\s*(?:[a-z/()-]+\s+){{0,4}}?(?:[a-z()-]+)?\s*{_SEP}(?P<unit>{_VOL_UNIT})\b",
    # "3,000 tickets monthly", "about 80 invoices weekly", "5000 transactions annually"
    rf"(?P<n>{_NUM})\s*(?:[a-z()-]+\s+){{0,4}}?(?P<unit>{_VOL_ADVERB})\b",
    # "every day roughly 300 calls come in", "daily about 50 requests"
    rf"\b(?:(?:every|each)\s+(?P<unit>{_VOL_UNIT})|(?P<unit2>{_VOL_ADVERB}))\b[^0-9.;\n]{{0,30}}?(?P<n>{_NUM})\s+[a-z]",
]
_PEAK_WORDS_RE = re.compile(r"\bpeaks?\b|\bmaxim\w+|\bmax\b|\bup to\b|\bat most\b|\bbusiest\b|\bworst\b")


def _monthly(n: float, unit: str) -> tuple[float, str]:
    """Convert a count per `unit` (day/daily, week, month, year...) to per month, with an explanation."""
    if unit.startswith("day") or unit.endswith("day") or unit == "daily":
        return n * WORKING_DAYS_PER_MONTH, f"{_g(n)} per day x {WORKING_DAYS_PER_MONTH} working days"
    if unit.startswith("week"):
        return round(n * WEEKS_PER_MONTH, 1), f"{_g(n)} per week x {WEEKS_PER_MONTH} weeks"
    if unit.startswith(("year", "annu")):
        return round(n / 12, 1), f"{_g(n)} per year / 12"
    return n, f"{_g(n)} per month"


def parse_volume(text: str) -> tuple[float, str] | None:
    """Monthly volume and an explanation: '4000 claims per month', '200 a day' (x22), '50 per week' (x4.33).

    Also reads '1,200 invoices/month', '3,000 tickets monthly' and 'every day about 300 calls'.
    A figure introduced as a peak or maximum is only used when no ordinary figure is given."""
    t = text.lower()
    found: list[tuple[int, float, str, bool]] = []  # (position, per month, explanation, is_peak)
    for pattern in _VOLUME_PATTERNS:
        for m in re.finditer(pattern, t):
            start = m.start("n")
            gd = m.groupdict()
            unit = (gd.get("unit") or gd.get("unit2") or "").strip()
            unit_pos = m.start("unit") if gd.get("unit") else m.start("unit2") if gd.get("unit2") else -1
            between = t[m.end("n"):unit_pos] if unit_pos > m.end("n") else ""
            after = t[m.end("n"):m.end("n") + 14]
            if re.search(r"\b(?:min|hour|hr|sec)|%|percent|" + _CUR, between):
                continue
            if re.search(_NOT_WORK_ITEMS, between):
                continue  # "5 days a week", "3 shifts per day", "4 people per team" are not volumes
            if unit_pos < m.end("n") and re.match(r"\s*(?:min|hour|hr|sec|%|percent|days?|weeks?|months?|years?|people|staff)", after):
                continue  # "every day 5 minutes ..." (the figure comes after the period)
            if re.search(_CUR + r"\s*$", t[max(0, start - 6):start]):
                continue  # it's a price like "AED 500 per month"
            n = parse_number(m.group("n"))
            if n is None:
                continue
            if not unit:
                continue
            before = re.split(r"[.;\n]|\bbut\b", t[max(0, start - 40):start])[-1]
            found.append((start, *_monthly(n, unit), bool(_PEAK_WORDS_RE.search(before))))
    if found:
        found.sort(key=lambda f: f[0])
        _, value, how, _ = next((f for f in found if not f[3]), found[0])
        return value, how
    m = re.search(rf"(daily|weekly|monthly|yearly|annual)\s+volume\s+(?:is\s+|of\s+)?(?:about\s+|around\s+|~\s*)?({_NUM})", t)
    if m:
        n = parse_number(m.group(2)) or 0
        factor = {"daily": WORKING_DAYS_PER_MONTH, "weekly": WEEKS_PER_MONTH, "monthly": 1}.get(m.group(1), 1 / 12)
        return round(n * factor, 1), f"{m.group(1)} volume {n:g}"
    return None


def parse_currency(text: str) -> str | None:
    """Currency next to a number ('AED 90', '$40', '40 euros'), else an unambiguous code or symbol."""
    t = text.lower()
    m = re.search(rf"({_CUR})\s*\d|(?<![\d,.])\d[\d,.]{{0,15}}\s*k?\s*({_CUR})", t)
    if m:
        return CURRENCY_SYMBOLS.get((m.group(1) or m.group(2)).strip().rstrip("."))
    m = re.search(r"us\$|\$|€|£|₹|(?<![a-z])(?:usd|aed|eur|gbp|sar|inr|dirhams?|euros?|dollars?|riyals?|rupees?|qar|kwd|bhd|omr|jod|egp|cad|aud|chf|sgd|jpy|cny|zar|pkr|nzd)(?![a-z])", t)
    return CURRENCY_SYMBOLS.get(m.group(0)) if m else None


def parse_hourly_cost(text: str) -> tuple[float, str | None] | None:
    """Fully loaded cost per hour and currency: 'AED 90 per hour', '$35/hour', '120 dirhams an hour'."""
    t = text.lower()
    per_hour = r"\s*(?:per|an|a|/|each)\s*(?:staff\s+|man[\s-]?)?(?:hour|hr|h)\b"
    m = re.search(rf"({_CUR})\s*({_NUM}){per_hour}", t) or re.search(rf"({_NUM})\s*({_CUR}){per_hour}", t)
    if m:
        cur_first = bool(re.match(_CUR, m.group(1)))
        cur, num = (m.group(1), m.group(2)) if cur_first else (m.group(2), m.group(1))
        amount = parse_number(num)
        if amount is not None:
            return amount, CURRENCY_SYMBOLS.get(cur.strip().rstrip("."))
    m = re.search(rf"(?:hourly (?:cost|rate)|cost per hour|rate per hour)\D{{0,20}}?({_CUR})?\s*({_NUM})\s*({_CUR})?", t)
    if m:
        amount = parse_number(m.group(2))
        cur = m.group(1) or m.group(3)
        if amount is not None:
            return amount, CURRENCY_SYMBOLS.get(cur.strip().rstrip(".")) if cur else None
    return None


def find_systems(text: str) -> list[str]:
    """System names mentioned in `text`, canonicalised, in order of appearance."""
    found: list[tuple[int, str]] = []
    t = text
    for name, (pattern, _, _) in KNOWN_SYSTEMS.items():
        m = re.search(rf"\b(?:{pattern})\b", t, re.I)
        if m:
            found.append((m.start(), _product_family(name, t, m.end())))
    m = re.search(r"\blegacy\s+((?:[a-z0-9&-]+\s+){0,2})" + SYSTEM_NOUNS + r"\b", t, re.I)
    if m:
        found.append((m.start(), _title(m.group(0))))
    elif re.search(r"\blegacy\b", t, re.I) and not any(n == "Mainframe" for _, n in found):
        lm = re.search(r"\blegacy\b", t, re.I)
        found.append((lm.start(), "Legacy System"))
    preps = r"\b(?:in|using|via|into|on|through|from|onto|within|inside)\s+(?:the\s+|our\s+|their\s+)?"
    for m in re.finditer(preps + r"([A-Z][\w&-]*(?:[ ]+[A-Z0-9][\w&-]*){0,3})", t):
        candidate = m.group(1).strip()
        if re.match(r"\s+(?:team|department|division|unit|office|region|branch)\b", t[m.end(1):], re.I):
            continue
        if candidate.lower().split()[0] in NOT_SYSTEMS or _known(candidate) or _known(candidate.split()[0]):
            continue
        if re.match(r"\s+" + SYSTEM_NOUNS + r"\b", t[m.end(1):], re.I):
            continue  # "the HR portal" is picked up whole by the lowercase pattern below
        if re.search(PERSON_OR_ROLE_RE, candidate, re.I):
            continue  # "from the Finance Director", "from Acme Corp"
        if m.group(0).lstrip().lower().startswith("from"):
            continue  # a capitalised name after "from" is usually a person or company, not software
        if len(candidate.split()) == 1 and re.match(r"\s+(?:via|by|about|regarding|and|who|said|asked)\b", t[m.end(1):], re.I):
            continue  # a lone first name ("John via email")
        found.append((m.start(1), candidate))
    for m in re.finditer(preps + r"((?:[a-z0-9&-]+\s+){0,2}" + SYSTEM_NOUNS + r")\b", t, re.I):
        candidate = m.group(1)
        words = candidate.lower().split()
        if words[0] in NOT_SYSTEMS or len(words) < 2 or re.match(r"legacy", candidate, re.I) or _known(candidate):
            continue
        found.append((m.start(1), _title(candidate)))
    seen: dict[str, None] = {}
    for _, name in sorted(found):
        if name.lower() not in {s.lower() for s in seen}:
            seen[name] = None
    return list(seen)


FAMILY_PRODUCTS = {"SAP", "Oracle", "Salesforce", "ServiceNow"}
_NOT_PRODUCT_WORD = {"then", "next", "and", "after", "also", "finally", "or", "to", "for", "the", "a", "an"}


def _product_family(name: str, text: str, end: int) -> str:
    """'SAP' followed by a capitalised product word ('SuccessFactors') is one system: 'SAP SuccessFactors'."""
    if name not in FAMILY_PRODUCTS:
        return name
    m = re.match(r"[ \t]+([A-Z][a-z]+[A-Za-z0-9]*)\b", text[end:])  # mixed case, so acronyms like EBS stay out
    if not m:
        return name
    word = m.group(1)
    if word.lower() in NOT_SYSTEMS | _NOT_PRODUCT_WORD or re.fullmatch(SYSTEM_NOUNS, word.lower()):
        return name
    return f"{name} {word}"


def input_type_of(text: str) -> InputType | None:
    t = text.lower()
    t = re.sub(r"e-?mail address(?:es)?|phone numbers?|mobile numbers?", " ", t)
    for kind, pattern in INPUT_KEYWORDS:
        if kind == "email" and re.match(r"\s*(?:send|reply|forward|email the|notify)", t):
            continue  # sending an email works from structured data
        if re.search(rf"\b(?:{pattern})", t):
            return kind
    return None


def mentions(text: str, pattern: str) -> bool | None:
    """True if `pattern` occurs un-negated, False if only negated occurrences, None if absent."""
    t = text.lower()
    hits = list(re.finditer(rf"\b(?:{pattern})", t))
    if not hits:
        return None
    for m in hits:
        before = t[max(0, m.start() - 30):m.start()]
        before = re.split(r"[.,;:!?]|\bbut\b|\bhowever\b", before)[-1]
        if not re.search(NEGATION_RE, before):
            return True
    return False


# ---------- main entry point ----------

def parse_process_text(text: str) -> ImportResult:
    """Extract whatever facts the text states. Unknowns stay None; every inference is noted."""
    notes: list[str] = []
    text = (text or "").replace("\r\n", "\n").replace("\t", " ")
    update = ProfileUpdate()
    if not text.strip():
        return ImportResult(update, ["The text was empty — nothing to import."])
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        notes.append(f"The text was longer than {MAX_TEXT_CHARS:,} characters, so only the first part was read.")

    text = _break_inline_enumerators(text)  # "1. A 2. B" on one line -> one step per line
    step_texts, prose = _split_steps(text)
    step_texts, trailers = _trim_trailing_facts(step_texts)
    step_texts, fact_sentences = _split_fact_sentences(step_texts)
    prose = prose + "\n" + trailers + "\n" + fact_sentences
    if len(step_texts) >= 2:
        notes.append(f"Found {len(step_texts)} steps.")
    elif step_texts:
        notes.append("Found only one step; add the others.")

    steps: list[Step] = []
    used: set[str] = set()
    for i, raw in enumerate(step_texts, 1):
        step = parse_step(raw, i, notes)
        base, n = step.name, 2
        while step.name.lower() in used:
            step.name = f"{base} ({n})"
            n += 1
        used.add(step.name.lower())
        steps.append(step)
    if steps:
        _apply_blanket_defaults(prose, steps, notes)
        _spread_total_minutes(prose, steps, notes)
        update.steps = steps

    # Process-level facts come from the whole text.
    _parse_process_facts(text, prose, update, notes)

    # Systems: everything mentioned, with known-product defaults.
    names: list[str] = []
    for n in [s.system for s in steps if s.system] + find_systems(text):
        if n.lower() not in {x.lower() for x in names}:
            names.append(n)
    systems = []
    for name in names:
        systems.append(system_facts(name, text, notes))
    if systems:
        update.systems = systems
        notes.append("Systems mentioned: " + ", ".join(s.name for s in systems) + ".")

    _parse_data(text, update, notes)
    if not steps:
        notes.append("No step list found — list the steps one per line, numbered, or joined with 'then'.")
    return ImportResult(update, notes)


# ---------- internals ----------

def _split_steps(text: str) -> tuple[list[str], str]:
    """Steps from a numbered/bulleted list, else from 'then/next' sentences. Returns (steps, remaining prose)."""
    items, prose_lines = [], []
    current: list[str] | None = None
    for line in _break_inline_enumerators(text).split("\n"):
        m = LIST_ITEM_RE.match(line)
        if m and len(m.group(1).strip()) > 1:
            if current:
                items.append(" ".join(current))
            current = [m.group(1).strip()]
        elif current is not None and line.startswith((" ", "  ")) and line.strip():
            current.append(line.strip())  # wrapped continuation of a list item
        else:
            if current:
                items.append(" ".join(current))
                current = None
            prose_lines.append(line)
    if current:
        items.append(" ".join(current))
    if len(items) >= 2:
        return items, "\n".join(prose_lines)

    if re.search(r"\b(?:then|next|after that|afterwards|finally|firstly)\b", text, re.I):
        parts = [p.strip(" ,:-") for p in SEQUENCE_SPLIT_RE.split(text) if p and p.strip(" ,:-")]
        parts = [c for p in parts for c in _split_clauses(p)]
        candidates = [p for p in parts if _looks_like_step(p)]
        if len(candidates) >= 2:
            rest = [p for p in parts if p not in candidates]
            return candidates, ". ".join(rest)

    # One sentence per step, each with its own time: "Buyer creates PO in SAP (8 min). Budget owner approves (2 min)."
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", text) if s.strip()]
    timed = [i for i, s in enumerate(sentences) if parse_minutes(s.lower()) is not None and _looks_like_step(s)]
    if len(timed) >= 2:
        span = range(timed[0], timed[-1] + 1)
        chosen = [i for i in span if i in timed or _looks_like_step(sentences[i])]
        return [sentences[i] for i in chosen], "\n".join(s for i, s in enumerate(sentences) if i not in chosen)
    return items, "\n".join(prose_lines)


# An enumerator ("1. ", "2) ", "Step 3: ") either starts a line or follows other text on the same line.
_ENUM_RE = re.compile(
    r"(?im)(?P<lead>^[ \t]*|(?<=\S)[ \t]+)(?:step\s*(?P<s>\d{1,2})\s*[:.)\-]\s*|(?P<b>\d{1,2})[.)]\s+)(?=[A-Za-z])"
)
_CLAUSE_START_RE = re.compile(
    r"(?:and\s+)?(?:(?:we|they|i|he|she|someone|somebody|staff|the staff|the team|our team)\s+\w+"
    r"|\w+(?:s|es)\s+(?:it|them|the|a|an|this|that|these|those|all|each|up|in|into)\b"
    r"|(?:download|key|type|enter|send|check|review|open|scan|update|create|copy|paste|forward|file|save|print|"
    r"upload|log|post|validate|verify|match|route|email|call|notify|book|register|extract|approve|read)\b)",
    re.I,
)


def _break_inline_enumerators(text: str) -> str:
    """'1. Read 2. Key 3. File' on one line -> one numbered step per line (needs 1, 2, ... in order)."""
    run = []
    for m in _ENUM_RE.finditer(text):
        if int(m.group("s") or m.group("b")) == len(run) + 1:
            run.append(m)
    inline = [m for m in run if m.start() > 0 and text[m.start() - 1] != "\n"]
    if len(run) < 2 or not inline:
        return text
    for m in reversed(inline):
        text = text[:m.start("lead")] + "\n" + text[m.end("lead"):]
    return text


def _split_clauses(part: str) -> list[str]:
    """Split 'AP gets invoices, someone downloads the pdf, types it into oracle' into separate step clauses."""
    pieces = [c.strip() for c in re.split(r",\s+", part) if c.strip()]
    if len(pieces) < 2 or not all(_CLAUSE_START_RE.match(c) for c in pieces[1:]):
        return [part]
    return pieces


def _trim_trailing_facts(items: list[str]) -> tuple[list[str], str]:
    """Cut ', about 4,000 claims a month, staff cost AED 90/hour' off the end of a step (kept as prose)."""
    out, trailers = [], []
    for item in items:
        for m in re.finditer(r",\s+", item):
            rest = item[m.end():]
            if parse_volume(rest.lower()) or parse_hourly_cost(rest.lower()):
                trailers.append(rest)
                item = item[:m.start()]
                break
        out.append(item)
    return out, "\n".join(trailers)


def _spread_total_minutes(prose: str, steps: list[Step], notes: list[str]) -> None:
    """A time stated for the whole item ('takes 10 min each') is shared across the steps that have no time."""
    total = _whole_process_minutes(prose)
    missing = [s for s in steps if s.minutes_per_run is None]
    if total is None or not missing:
        return
    left = total - sum(s.minutes_per_run or 0 for s in steps)
    if left <= 0:
        return
    share = round(left / len(missing), 2)
    for s in missing[:-1]:
        s.minutes_per_run = share
    missing[-1].minutes_per_run = round(left - share * (len(missing) - 1), 2)
    notes.append(
        f"You gave {total:g} min per item for the whole process: spread {left:g} min evenly over "
        f"{len(missing)} step(s) without a time (correct any that differ)."
    )


def _apply_blanket_defaults(prose: str, steps: list[Step], notes: list[str]) -> None:
    """Facts stated for the whole process ('all inputs are structured forms, no judgment needed') fill unset step fields."""
    low = prose.lower()
    applied: list[str] = []

    def fill(field: str, value, label: str) -> None:
        todo = [st for st in steps if getattr(st, field) is None]
        if todo:
            for st in todo:
                setattr(st, field, value)
            applied.append(label)

    m = re.search(r"(?:inputs?|everything|all (?:the )?(?:data|inputs?))\s+(?:are|is)\s+([^.;\n]+)", low)
    kind = input_type_of(m.group(1)) if m else None
    if kind:
        fill("input_type", kind, f"inputs are {kind.replace('_', ' ')}")
    if re.search(r"no (?:human )?judg(?:e)?ment|(?:needs?|requires?) no judg(?:e)?ment", low):
        fill("requires_judgment", False, "no judgment needed")
    if re.search(RULES_NO_RE, low):
        fill("rules_documented", False, "rules not documented")
    elif re.search(r"rules? (?:are |is )?(?:all )?(?:documented|written)|documented rules", low):
        fill("rules_documented", True, "rules documented")
    m = re.search(r"exceptions?\s*(?:rate)?\s*(?:is|of|at|about|around|approx\w*|roughly|~|:|\s)*\s*(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*(?:%|percent)", low)
    if m:
        fill("exception_rate_pct", min(float(m.group(1)), 100.0), f"exceptions about {m.group(1)}%")
    if applied:
        notes.append("Applied to every step with no value yet (stated for the whole process): " + "; ".join(applied) + ".")


def _looks_like_step(part: str) -> bool:
    words = part.split()
    if len(words) < 2 or len(words) > 40:
        return False
    low = part.lower()
    # Sentences that only state volume/cost/data facts are not steps.
    if parse_volume(low) or parse_hourly_cost(low):
        return False
    if _is_fact_sentence(part) or _ARRIVAL_RE.search(low):
        return False
    return not re.match(r"(?:it|this|there|the data|data|volume|cost|contains?|includes?|including|must|should)\b", low)


# "Referral letters arrive by email": an inbound event, not something a person does.
_ARRIVAL_RE = re.compile(r"^[\w\s,'/-]{0,50}?\b(?:arrives?|comes? in|comes? through|(?:is|are) received|gets? sent in)\b")
_FACT_SENTENCE_RE = re.compile(
    r"\b(?:everything|all (?:the )?(?:inputs?|rules|steps|data)|(?:the )?(?:process|rules) (?:rarely|never|seldom|often)|"
    r"(?:each|every) \w+ takes|in total|overall|altogether|we (?:get|handle|process|receive)|(?:staff|labou?r|coordinator|hourly) cost|"
    r"costs? (?:is|are)|no (?:personal|patient|health|pii|phi)|must stay|(?:personal|patient|health|customer) data|"
    r"residency|emirates id|peaks?)\b",
    re.I,
)


def _is_fact_sentence(sentence: str) -> bool:
    """A sentence that states a whole-process fact (volume, cost, data, time in total), not an action."""
    low = sentence.lower()
    return bool(
        parse_volume(low)
        or parse_hourly_cost(low)
        or re.match(r"\s*(?:about|around|roughly|approximately|approx\.?|~)\s*[\d$€£]", low)
        or _FACT_SENTENCE_RE.search(sentence)
    )


def _split_fact_sentences(items: list[str]) -> tuple[list[str], str]:
    """Cut trailing fact sentences off a step ('... (1 min). About 120 a month. Staff cost is $40 per hour.')."""
    out, facts = [], []
    for item in items:
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9$€£~])", item)
        cut = next((i for i in range(1, len(parts)) if _is_fact_sentence(parts[i])), None)
        if cut is not None:
            facts.extend(parts[cut:])
            item = " ".join(parts[:cut])
        out.append(item.rstrip(" .;"))
    return out, "\n".join(facts)


_TOTAL_TIME_AFTER_RE = re.compile(r"\s*(?:each|per\s+\w+|in total|overall|end[ -]to[ -]end|altogether|in all|all in)\b")
_TOTAL_TIME_BEFORE_RE = re.compile(r"(?:takes?|taking|needs?|requires?)\s*(?:about|around|roughly|maybe|approx\w*|~|up to)?\s*(?:about|maybe)?\s*$")


def _whole_process_minutes(prose: str) -> float | None:
    """Handling time stated for the whole item ('takes 10 mins each', 'about 20 minutes in total')."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n", prose):
        low = sentence.lower()
        if "step" in low:
            continue
        for m in _DURATION_RE.finditer(low):
            before, after = low[:m.start()], low[m.end():]
            if _TOTAL_TIME_BEFORE_RE.search(before[-40:]) and (
                _TOTAL_TIME_AFTER_RE.match(after) or re.match(r"\s*(?:each|every|one)\b", before)
            ):
                return parse_minutes(low[max(0, m.start() - 12):m.end() + 12]) or parse_minutes(low)
    return None


def parse_step(raw: str, index: int = 1, notes: list[str] | None = None) -> Step:
    """One step from a line of text (name, minutes, system, input type, flags)."""
    notes = notes if notes is not None else []
    text = raw.strip()
    low = text.lower()
    minutes = parse_minutes(low)
    systems = find_systems(text)
    system = systems[0] if systems else None
    input_type = input_type_of(text)
    approval = mentions(low, APPROVAL_RE)
    judgment = mentions(low, JUDGMENT_RE)
    if re.search(r"no (?:human )?judg(?:e)?ment|rule[\s-]based|straightforward|mechanical", low):
        judgment = False
    rules = False if re.search(RULES_NO_RE, low) else (True if re.search(rf"\b(?:{RULES_YES_RE})", low) else None)
    exception = None
    m = re.search(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*(?:%|percent)", low)
    if m and re.search(r"exception|reject|fail|escalat|wrong|return|rework|bounce|missing|incomplete|query|queries|manual", low):
        exception = min(float(m.group(1)), 100.0)
    language = None
    if input_type in ("email", "unstructured", "voice") and mentions(low, LANGUAGE_RE):
        language = True

    name = _step_name(text, system, index)
    if approval:
        notes.append(f"'{name}' looks like an approval step (kept with a person).")
    if minutes is not None:
        notes.append(f"'{name}': {minutes:g} min per run.")
    return Step(
        name=name,
        description=text if len(text) > len(name) + 3 else None,
        minutes_per_run=minutes,
        system=system,
        input_type=input_type,
        requires_judgment=judgment,
        needs_language_understanding=language,
        rules_documented=rules,
        exception_rate_pct=exception,
        is_approval=approval,
    )


def _step_name(text: str, system: str | None, index: int) -> str:
    # a comma between digits is a thousands separator ("over 5,000"), not a clause break
    name = re.split(r"\s+[-–—]\s+|:|\(|;|\[|,(?!\d)|\s+(?:which|that takes|taking|takes|usually|normally|typically)\b", text)[0]
    name = re.sub(
        r"\b(?:takes?|taking|takes about|about|around|approx\.?|roughly|~)?\s*(?:\d+(?:\.\d+)?\s*(?:-|–|to)\s*)?"
        r"\d+(?:\.\d+)?\s*(?:minutes?|mins?|hours?|hrs?|seconds?|secs?)\b",
        "",
        name,
        flags=re.I,
    )
    name = re.sub(r"\b(?:takes? )?half an hour\b", "", name, flags=re.I)
    name = _tidy_name(name)
    if system:
        surface = KNOWN_SYSTEMS[system][0] if system in KNOWN_SYSTEMS else re.escape(system)
        stripped = _tidy_name(re.sub(
            r"\s+\b(?:in|using|via|into|on|onto|through|within|inside|from)\s+(?:the\s+|our\s+|their\s+)?(?:"
            + surface + r")\b",
            "", name, flags=re.I,
        ))
        last = stripped.split()[-1].lower() if stripped else ""
        # keep the system when removing it would leave "Types it", a dangling "from" or a bare verb ("Enter", "Pay")
        if stripped and len(stripped.split()) >= 2 and last not in PREPOSITIONS | {"it", "them", "this", "that"}:
            name = stripped
    if not name:
        return f"Step {index}"
    words = name.split()
    if len(words) > 8:
        # cut at the first preposition ("... from the scanned referral in Epic") rather than mid-phrase
        cut = next((i for i, w in enumerate(words) if i >= 3 and w.lower() in PREPOSITIONS), 8)
        words = words[:min(cut, 8)]
    while len(words) > 1 and words[-1].lower() in PREPOSITIONS | {"the", "a", "an", "and", "or", "to"}:
        words.pop()
    name = " ".join(words)
    return name[0].upper() + name[1:]


def _tidy_name(name: str) -> str:
    """Drop filler ('so basically'), a leading subject ('we', 'someone') and articles."""
    for _ in range(2):
        name = FILLER_RE.sub("", name.strip())
        name = SUBJECT_RE.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" .,-–")
    return re.sub(r"^(?:the|a|an)\s+", "", name, flags=re.I)


def system_facts(name: str, text: str = "", notes: list[str] | None = None) -> System:
    """System with defaults for well-known products, overridden by what `text` says about it."""
    notes = notes if notes is not None else []
    has_api = legacy = ui = None
    known = KNOWN_SYSTEMS.get(name) or (KNOWN_SYSTEMS.get(name.split()[0]) if name.split()[0] in FAMILY_PRODUCTS else None)
    if known:
        has_api, legacy = known[1], known[2]
        if has_api:
            notes.append(f"Assumed {name} has a Power Automate connector (standard product).")
    if name.lower().startswith("legacy") or name == "Mainframe":
        has_api, legacy = False, True
        notes.append(f"Assumed {name} is a legacy app with no API (reached through its screens).")
    # Sentences that talk about this system can override the defaults.
    for sentence in re.split(r"[.;\n!?]", text):
        if name.lower() not in sentence.lower():
            continue
        s = sentence.lower()
        if re.search(r"no (?:api|connector|integration)|without (?:an? )?(?:api|connector)|doesn't have an? (?:api|connector)|does not have an? (?:api|connector)", s):
            has_api = False
        elif re.search(r"\b(?:rest |web |has an? |with an? )?api\b|connector|web service", s):
            has_api = True
        if re.search(r"desktop|citrix|green[\s-]screen|terminal|thick client", s):
            legacy = True
        if re.search(r"screens? (?:change|are updated) (?:often|frequently|a lot|every (?:week|month))", s):
            ui = "frequent"
        elif re.search(r"screens? (?:rarely|never|hardly ever) change", s):
            ui = "rare"
    return System(name=name, has_api_or_connector=has_api, ui_change_frequency=ui, is_legacy_desktop=legacy)


# A heading that only introduces the list ("Steps:", "Here are the steps:") is not a process name.
_GENERIC_HEADING_RE = re.compile(
    r"^[#*\s]*(?:here(?:'s| is| are)\s+(?:the\s+|my\s+|our\s+)?|the\s+|my\s+|our\s+)?"
    r"(?:process\s+)?(?:steps?|process|procedure|workflow|overview|sop|description|details|summary|instructions?|tasks?)"
    r"(?:\s+(?:are|is|below|follows?|as follows|to follow|in order))*[\s:.\-*]*$",
    re.I,
)


def _parse_process_facts(text: str, prose: str, update: ProfileUpdate, notes: list[str]) -> None:
    low = text.lower()
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    m = re.search(r"(?:process(?: name)?|name of (?:the )?process|title)\s*[:=\-]\s*(.+)", text, re.I)
    if m:
        update.name = _clean_title(m.group(1))
    elif (
        len(lines) >= 2 and len(lines[0]) <= 80 and not LIST_ITEM_RE.match(lines[0])
        and not lines[0].rstrip().endswith((".", "?", "!")) and len(lines[0].split()) <= 10
        and not _GENERIC_HEADING_RE.match(lines[0].strip())
    ):
        update.name = _clean_title(lines[0])
    else:
        m = re.search(r"\b(?:the|our|this|a|an)\s+((?:[a-z0-9&/-]+\s+){0,3}?[a-z0-9&/-]+)\s+process\b", low)
        if m and m.group(1).split()[0] not in ("whole", "entire", "same", "current", "manual", "automation", "this"):
            update.name = m.group(1).strip().capitalize()
    if update.name:
        notes.append(f"Process name: '{update.name}'.")

    sentences = [
        s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", prose)
        if len(s.strip().split()) >= 5 and not _is_fact_sentence(s)  # facts have their own fields
    ]
    if sentences:
        update.description = " ".join(sentences)[:500]

    vol = parse_volume(low)
    if vol:
        update.transactions_per_month = vol[0]
        how = "" if vol[1] == f"{_g(vol[0])} per month" else f" ({vol[1]})"
        notes.append(f"Volume: {_g(vol[0])} per month{how}.")
    cost = parse_hourly_cost(low)
    if cost:
        update.hourly_labor_cost = cost[0]
        if cost[1]:
            update.currency = cost[1]
        notes.append(f"Hourly staff cost: {cost[0]:g}" + (f" {cost[1]}" if cost[1] else " (currency not stated)") + ".")
    if not update.currency:
        cur = parse_currency(low) if re.search(r"\d", low) else None
        if cur and re.search(rf"{_CUR}\s*\d|\d\s*{_CUR}", low):
            update.currency = cur
            notes.append(f"Currency: {cur}.")

    m = re.search(r"(?:error|rework|mistake)s?(?: rate)?(?:\s*(?:is|of|at|about|around|approx\w*|roughly|~|:))*\s*(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*(?:%|per\s?cent)", low) or re.search(
        r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*(?:%|per\s?cent)\s*(?:of [a-z ]{0,20})?(?:have |has |contain |are |need |get |got )?"
        r"(?:errors?|mistakes?|rework|re-?worked|rejected|returned|bounced)", low
    )
    if m:
        update.error_rate_pct = min(float(m.group(1)), 100.0)
        notes.append(f"Current error/rework rate: {update.error_rate_pct:g}%.")

    for value, pattern in STABILITY_PATTERNS:
        if re.search(pattern, low) and re.search(r"process|rules?|change|stable|form", low):
            update.process_stability = value
            notes.append(f"Process stability: {value.replace('_', ' ')}.")
            break

    m = re.search(r"\b((?:[A-Z][\w&]*\s+){1,3})(?:department|team|division|unit)\b", text)
    if m and m.group(1).split()[0].lower() not in NOT_SYSTEMS:
        update.department = m.group(1).strip()
    if re.search(r"insur", low) and not re.search(r"health\w* insur|medical insur", low):
        # a hospital/clinic that checks insurance is healthcare, unless insurance clearly dominates
        if len(re.findall(r"hospital|clinic|patient|healthcare", low)) >= len(re.findall(r"insur", low)) > 0:
            update.industry = "Healthcare"
    for industry, pattern in [] if update.industry else [
        ("Healthcare insurance", r"health\w* insur|medical insur"),
        ("Insurance", r"insur"),
        ("Healthcare", r"hospital|clinic|patient|healthcare"),
        ("Banking", r"\bbank|lending|loan"),
        ("Government", r"government|ministry|municipal"),
        ("Retail", r"retail|e-?commerce|store"),
        ("Logistics", r"logistic|shipping|freight|warehouse"),
    ]:
        if re.search(pattern, low):
            update.industry = industry
            break
    # Find the keyword first, then take its sentence (a leading [^.]* scan is quadratic on long text).
    m = next((re.search(r"[^.\n]*\bpeaks?\b[^.\n]*", s, re.I) for s in re.split(r"[.\n]", text) if re.search(r"\bpeaks?\b", s, re.I)), None)
    if m:
        update.frequency = m.group(0).strip()[:120]


def _parse_data(text: str, update: ProfileUpdate, notes: list[str]) -> None:
    low = re.sub(r"(?:process|step|file|system|user|team|department)\s+names?|name of the \w+", " ", text.lower())
    data = DataProfile()
    phi = mentions(low, PHI_RE)
    if phi is not None:
        data.contains_phi = phi
        notes.append("Health/patient data mentioned -> PHI." if phi else "Text says there is no health data -> no PHI.")
    pii = mentions(low, PII_RE)
    if pii is not None:
        data.contains_pii = pii
        notes.append("Personal identifiers mentioned -> PII." if pii else "Text says there is no personal data -> no PII.")
    elif phi:
        data.contains_pii = True
        notes.append("Assumed PII too: patient data normally identifies a person.")
    if (
        phi and data.contains_pii is False
        and not re.search(r"(?:no|without)\s+(?:any\s+)?(?:personal|identifiable)\s+(?:data|information)|de-?identified|anonymi[sz]ed", low)
    ):
        # "we do not store patient names outside the hospital" still shows that names are handled
        data.contains_pii = True
        notes.append("Assumed PII too: patient data normally identifies a person.")
    uae = bool(re.search(UAE_RE, low))
    if re.search(RESIDENCY_NO_RE, low):
        data.uae_data_residency_required = False
        notes.append("Text says data may be stored outside the country.")
    elif re.search(RESIDENCY_YES_RE, low):
        data.uae_data_residency_required = True
        notes.append("Data must stay in the UAE (stated).")
    elif uae and phi:
        data.uae_data_residency_required = True
        notes.append("Assumed UAE data residency: UAE health data generally must stay in the UAE (verify with legal).")
    elif uae:
        notes.append("UAE mentioned — confirm whether the data must stay in the UAE.")
    if data.model_dump(exclude_none=True):
        update.data = data


def _known(candidate: str) -> bool:
    return any(re.fullmatch(rf"(?:{p})", candidate, re.I) for p, _, _ in KNOWN_SYSTEMS.values())


def _title(s: str) -> str:
    small = {"crm", "erp", "db", "hr"}
    return " ".join(w.upper() if w.lower() in small else (w if w[:1].isupper() else w.capitalize()) for w in s.split())


def _clean_title(s: str) -> str:
    s = re.sub(r"^[#*\s]+|[*\s:]+$", "", s.strip())
    return s[:80]
