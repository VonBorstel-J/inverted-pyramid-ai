"""Post-checks on a written answer (deterministic, no model):

1. citations: every [n] marker must name a provided source, and every
   full-form citation [doc_id, section, effective_date] must be one of the
   provided citations; a non-refusal answer must cite at least once;
2. numbers: every number in the answer must equal a value in the SQL results
   (to the precision written; a percent may be a rate x 100), or appear in a
   cited document passage, or in the question itself;
3. grounding: every sentence that states a number carries a citation.

Identifiers (N412AM, MX-2026-000001, DEN-01, Sec 4.2, CO-50) are not numbers.
ISO dates must be dates found in the SQL results, the passages' citations,
the question or as_of.
"""
import datetime as dt
import re

MARKER = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")
FULL_CITE = re.compile(r"\[([A-Za-z][\w.\-]*),\s*([^,\]]+?),\s*(\d{4}-\d{2}-\d{2}|None)\]")
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\b")
IDENT = re.compile(r"\b[A-Z][A-Z0-9]{0,6}(?:-[A-Z0-9]+)+\b"     # MX-2026-000001, DEN-01, SOP-0-B, CO-50
                   r"|\bN\d{3}[A-Z]{2}\b"                       # tail numbers
                   r"|\b[A-Z]\d{2}(?:\.\w+)?\b|\bA0\d{3}\b"     # ICD-10 / HCPCS codes
                   r"|(?:\bSec(?:tion)?\.?|§)\s*\d+(?:\.\d+)*[a-z]?", re.I)
NUMBER = re.compile(r"(?<![\w.])[-−]?\$?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|(?<![\w.])[-−]?\$?\d+(?:\.\d+)?%?")
TAG = re.compile(r"\[[A-Z_]+\]")  # redaction tags such as [NAME]


def strip_citations(text):
    return MARKER.sub(" ", FULL_CITE.sub(" ", text or ""))


def numbers(text):
    """[(token, value, decimals, is_percent)] in text, identifiers, citations,
    redaction tags and ISO dates removed first."""
    t = strip_citations(text)
    t = TAG.sub(" ", t)
    t = ISO_DATE.sub(" ", t)
    t = IDENT.sub(" ", t)
    out = []
    for m in NUMBER.finditer(t):
        tok = m.group(0)
        raw = tok.replace("$", "").replace(",", "").replace("%", "").replace("−", "-")
        try:
            v = float(raw)
        except ValueError:
            continue
        dec = len(raw.split(".")[1]) if "." in raw else 0
        out.append((tok, v, dec, tok.endswith("%")))
    return out


def dates(text):
    return {m.group(0)[:10] for m in ISO_DATE.finditer(text or "")}


def values_from_rows(rows):
    """Numeric values and date strings found in SQL result rows."""
    nums, ds = set(), set()
    for r in rows:
        for v in r.values():
            if isinstance(v, bool) or v is None:
                continue
            if isinstance(v, (int, float)):
                nums.add(float(v))
            elif hasattr(v, "is_finite") or type(v).__name__ == "Decimal":
                nums.add(float(v))
            elif isinstance(v, (dt.date, dt.datetime)):
                d = v.date() if isinstance(v, dt.datetime) else v
                ds.add(d.isoformat())
                nums.update({float(d.year), float(d.day)})
                if isinstance(v, dt.datetime):
                    nums.update({float(v.hour), float(v.minute)})
    return nums, ds


def matches(value, dec, is_pct, allowed):
    for a in allowed:
        if abs(a - value) < 1e-9 or round(a, dec) == value or abs(round(a, dec) - value) < 1e-9:
            return True
        if is_pct and (abs(round(a * 100, dec) - value) < 1e-9):
            return True
    return False


QUOTED = re.compile(r'"[^"\n]*"|\u201c[^\u201d\n]*\u201d')


def sentences(text):
    """Sentences of text; a quoted passage stays inside its sentence."""
    t = QUOTED.sub(lambda m: m.group(0).replace(". ", ".\x00").replace("! ", "!\x00").replace("? ", "?\x00"),
                   (text or "").strip())
    parts = [s.replace("\x00", " ") for s in re.split(r"(?<=[.!?])\s+|\n+", t) if s.strip()]
    out = []
    for p in parts:  # a sentence made only of citation markers belongs to the previous one
        if out and not strip_citations(p).strip(" .;,"):
            out[-1] += " " + p
        else:
            out.append(p)
    return out


def check(answer, sources, sql_rows, question="", as_of=None):
    """sources: [{"n": 1, "citation": [...], "kind": "sql"|"doc", "text": ...}].
    Returns {"ok", "problems", "cited", "numbers"}."""
    problems = []
    by_n = {s["n"]: s for s in sources}
    valid_full = {tuple(str(x) for x in s["citation"]) for s in sources}
    cited = set()
    for m in MARKER.finditer(answer or ""):
        for n in re.split(r"\s*[,;]\s*", m.group(1)):
            n = int(n)
            if n not in by_n:
                problems.append(f"citation [{n}] is not a provided source")
            else:
                cited.add(n)
    for m in FULL_CITE.finditer(answer or ""):
        c = (m.group(1), m.group(2).strip(), m.group(3))
        hit = [s["n"] for s in sources if tuple(str(x) for x in s["citation"]) == c]
        if not hit:
            problems.append(f"citation [{', '.join(c)}] is not in the retrieved set")
        cited.update(hit)
    if not cited:
        problems.append("the answer cites no source")
    allowed, allowed_dates = values_from_rows(sql_rows)
    for s in sources:
        allowed_dates.update(str(x) for x in s["citation"] if x and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(x)))
        if s["kind"] == "doc" and s["n"] in cited:
            allowed.update(v for _, v, _, _ in numbers(s.get("text", "")))
    allowed.update(v for _, v, _, _ in numbers(question))
    allowed_dates |= dates(question)
    if as_of:
        allowed_dates.add(str(as_of)[:10])
        d = as_of if isinstance(as_of, dt.date) else dt.date.fromisoformat(str(as_of)[:10])
        allowed.update({float(d.year), float(d.day)})
    found = []
    for tok, v, dec, pct in numbers(answer):
        ok = matches(v, dec, pct, allowed)
        found.append({"token": tok, "value": v, "ok": ok})
        if not ok:
            problems.append(f"number {tok} is not in the SQL results or a cited passage")
    for d in dates(answer):
        if d not in allowed_dates:
            problems.append(f"date {d} is not in the SQL results or citations")
    for s in sentences(answer):
        if numbers(s) and not (MARKER.search(s) or FULL_CITE.search(s)):
            problems.append(f"uncited number in: {s[:80]!r}")
    return {"ok": not problems, "problems": problems, "cited": sorted(cited), "numbers": found}
