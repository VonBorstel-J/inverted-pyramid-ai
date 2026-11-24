"""Question planning: classify (router task answer.classify, rules baseline +
model), pull entities with regex, and pick governed queries from a fixed
catalog. Models never write SQL here and never produce a number: every number
comes from a gold view read through governance.roles.query as the caller's role.
"""
import calendar
import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

import llm

CLASSIFY_TASK = "answer.classify"
KINDS = ("metric", "document", "cross_department")


class QuestionKind(BaseModel):
    kind: Literal["metric", "document", "cross_department"]


CLASSIFY_PROMPT = ("answer_classify.v1", """Classify a question about a synthetic air-medical transport company's data.
- "metric": asks for a number from one department's data (counts, rates, totals, revenue, overtime hours, flags).
- "document": asks what a policy, SOP or handbook says (rules, limits, requirements, procedures).
- "cross_department": asks for the combined impact of one event across departments (for example what an
  aircraft's AOG / grounding cost: missed transports, lost revenue, crew overtime, claims at risk).

Question: {q}""")

TAIL = re.compile(r"\b(N\d{3}[A-Z]{2})\b")
BASE = re.compile(r"\b([A-Z]{3}-\d{2})\b")
ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTH_RE = re.compile(r"\b(" + "|".join(sorted((m for m in MONTHS if len(m) > 3 or m in ("jun", "jul", "aug", "sep", "oct")),
                                               key=len, reverse=True)) + r")\b(?:\s+(\d{4}))?", re.I)
CROSS_EVENT = re.compile(r"\b(AOG|aircraft on ground|grounded|grounding|outage)\b", re.I)
COST = re.compile(r"\b(cost|costs|cost us|impact|lose|lost|missed|overtime|claims?|revenue|damage|price)\b", re.I)
METRIC = re.compile(r"\b(how many|how much|how far|number of|count|total|sum|average|percent|percentage|variance|"
                    r"revenue|overtime hours|at[- ]risk|flags?|expired|expiring|miss(?:ed)? plan|over plan|under plan|"
                    r"(?:at[- ]risk|denial|claim) rate)\b", re.I)
DOC_CUE = re.compile(r"\b(policy|policies|sop|handbook|procedure|memo|requires?|required|rules?|allowed|may|must|"
                     r"do we (?:charge|assign|use|record)|hourly|per hour|per shift|limit)\b", re.I)
DEPT_WORDS = {"BILLING": r"claims?|denial|payer|billed", "OPS": r"AOG|dispatch|transports?|tail|aircraft",
              "FINANCE": r"revenue|cost|budget|plan|ledger", "HR": r"overtime|crew|licen[cs]e|duty"}


def rules_classify(q):
    if COST.search(q) and (TAIL.search(q) or (CROSS_EVENT.search(q) and not DOC_CUE.search(q))):
        return {"kind": "cross_department"}
    depts = sum(1 for p in DEPT_WORDS.values() if re.search(rf"\b({p})\b", q, re.I))
    if depts >= 3 and METRIC.search(q):
        return {"kind": "cross_department"}
    scoped = TAIL.search(q) or BASE.search(q) or MONTH_RE.search(q)
    if METRIC.search(q) and (scoped or not DOC_CUE.search(q)):
        return {"kind": "metric"}
    return {"kind": "document"}


def classify(q, provider="rules", model=None, **opts):
    """Router task answer.classify. Falls back to the rules answer when the
    model fails (the failure is still logged by the router)."""
    try:
        got = llm.call(CLASSIFY_TASK, q if provider == "rules" else CLASSIFY_PROMPT[1].format(q=q),
                       provider=provider, schema=QuestionKind, model=model,
                       rules=lambda _p: rules_classify(q), **opts)
        return got.kind, provider
    except Exception:
        return rules_classify(q)["kind"], "rules(fallback)"


@dataclass
class Entities:
    tail: str = None
    base: str = None
    month: dt.date = None       # first day of the month the question names
    date: dt.date = None        # an explicit ISO date in the question
    words: set = field(default_factory=set)


def entities(q, year=None):
    e = Entities()
    m = TAIL.search(q)
    e.tail = m.group(1) if m else None
    m = BASE.search(q)
    e.base = m.group(1) if m else None
    m = ISO.search(q)
    if m:
        e.date = dt.date.fromisoformat(m.group(1))
    m = MONTH_RE.search(q)
    if m and not (m.group(1).lower() == "may" and not m.group(2)):  # "may" is usually the verb
        y = int(m.group(2)) if m.group(2) else (year or 2026)
        e.month = dt.date(y, MONTHS[m.group(1).lower()], 1)
    e.words = set(re.findall(r"[a-z]+", q.lower()))
    return e


def default_as_of(e, as_of=None):
    """The question's date: explicit as_of, else an ISO date in the question,
    else the end of the month it names, else today."""
    if as_of:
        return as_of if isinstance(as_of, dt.date) else dt.date.fromisoformat(str(as_of)[:10])
    if e.date:
        return e.date
    if e.month:
        return dt.date(e.month.year, e.month.month, calendar.monthrange(e.month.year, e.month.month)[1])
    return dt.date.today()


# ------------------------------------------------------------ governed query catalog
@dataclass
class Q:
    view: str
    triggers: str            # regex on the question
    filters: tuple           # entity -> column: subset of ("tail", "base", "month")
    key: tuple               # columns that identify a row (for its citation)
    order: str = ""
    additive: tuple = ()     # columns a SQL total may sum


CATALOG = [
    Q("aog_events", r"\b(aog|grounded|grounding|outage|lost transports?|missed transports?|lost revenue|tail)\b",
      ("tail", "base", "month"), ("mx_id",), "month, tail_number",
      ("aog_hours", "est_lost_transports", "est_lost_revenue", "overtime_hours", "overtime_cost", "est_total_cost")),
    Q("claims_at_risk_by_base_month", r"\b(claims?|at risk|denials?|billed)\b", ("base", "month"), ("base_id", "month"),
      "month, base_id", ("claims", "claims_at_risk", "billed_at_risk")),
    Q("claims_at_risk_by_reason", r"\b(reasons?|why)\b.*\bclaims?\b|\bclaims?\b.*\b(reasons?|why)\b", (), ("reason", "policy_doc_id"),
      "claims DESC", ("claims",)),
    Q("revenue_by_base_month", r"\b(revenue|plan|budget)\b", ("base", "month"), ("base_id", "month"), "month, base_id",
      ("revenue", "revenue_planned", "revenue_variance")),
    Q("variance_drivers", r"\b(variances?|drivers?|over budget|under budget|missed plan)\b", ("base", "month"),
      ("base_id", "month", "line"), "month, base_id, line", ("variance",)),
    Q("overtime_by_cause", r"\b(overtime|ot)\b", ("base", "month"), ("base_id", "month", "cause"), "month, base_id, cause",
      ("overtime_hours", "shifts")),
    Q("compliance_by_base_month", r"\b(compliance|flags?|duty[- ]limit|breach(?:es)?|expired licen[cs]es? on)\b",
      ("base", "month"), ("base_id", "month", "flag"), "month, base_id, flag", ("flags",)),
    Q("license_status_by_base", r"\b(licen[cs]es?|credentials?|expir\w*)\b", ("base",), ("base_id", "status"),
      "base_id, status", ("licenses", "expiring_in_window")),
]
COLUMN = {"tail": "tail_number", "base": "base_id", "month": "month"}
MAX_ROWS = 8


def pick_queries(q, kind):
    if kind == "document":
        return []
    if kind == "cross_department":
        return [CATALOG[0]]
    hits = [c for c in CATALOG if re.search(c.triggers, q, re.I)]
    # the more specific claims-by-reason view wins over the base/month one when both match
    if any(c.view == "claims_at_risk_by_reason" for c in hits):
        hits = [c for c in hits if c.view != "claims_at_risk_by_base_month"]
    return hits[:2]


def run_queries(con, role, queries, ents):
    """[(Q, rows, total_row_or_None, error)] read through the role's schema."""
    from governance import roles
    out = []
    for c in queries:
        where, params = [], []
        for ent in c.filters:
            v = getattr(ents, ent)
            if v is not None:
                where.append(f"{COLUMN[ent]} = ?")
                params.append(v)
        sql = f"SELECT * FROM {c.view}" + (" WHERE " + " AND ".join(where) if where else "") + \
              (f" ORDER BY {c.order}" if c.order else "")
        try:
            rows = roles.query(con, role, sql, params)
        except Exception as e:  # AccessDenied or a view this role does not have
            out.append((c, [], None, f"{type(e).__name__}: {str(e)[:200]}"))
            continue
        total = None
        if len(rows) > 1 and c.additive:
            cols = [a for a in c.additive if a in rows[0]]
            if cols:
                tsql = (f"SELECT {', '.join(f'sum({a}) AS {a}' for a in cols)}, count(*) AS n_rows FROM {c.view}"
                        + (" WHERE " + " AND ".join(where) if where else ""))
                total = roles.query(con, role, tsql, params)[0]
        out.append((c, rows[:MAX_ROWS], total, None))
    return out


def search_queries(question, kind):
    """The search strings used for a question (evals warm the query-embedding
    cache with exactly these)."""
    qs = [question]
    if kind == "cross_department":
        qs += ["expected net receipts for each missed AOG request",
               "recovery overtime rate per hour",
               "payer policy documentation required for air transport claims"]
    return qs
