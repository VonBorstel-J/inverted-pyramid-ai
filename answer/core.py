"""`ask(question, role, as_of=None, provider=...)`: governed question answering.

1. classify the question (router task answer.classify: rules baseline + model);
2. NUMBERS only from governed gold views, read as `role` through
   governance.roles.query (fixed query catalog in answer/plan.py);
3. passages from hybrid search (ACTIVE, effective by the question's date,
   scoped to the role);
4. the model (router task answer.write) writes prose citing only the
   provided sources; the `rules` provider writes a deterministic template;
5. post-check (answer/check.py): any number not in the SQL results (or a cited
   passage), any citation not provided, or an uncited number sentence rejects
   the answer: one retry with the problems listed, then the refusal
   "I can't answer that from governed data." rather than a guess.
"""
import datetime as dt
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import llm
from answer import check as C
from answer import plan as PL
from llm.phi import find_phi

REPO = Path(__file__).resolve().parent.parent
REFUSAL = "I can't answer that from governed data."
WRITE_TASK = "answer.write"
WRITE_PROMPT = ("answer_write.v1", """You answer a question for the {role} team of a synthetic air-medical transport company
using ONLY the numbered sources below.
Rules:
- Copy every number exactly as it is written in a source. Never compute, add, round or convert a number.
- End every sentence that states a number or a fact with its source marker, like [1] or [1][3].
- Cite only the markers listed below. Do not invent sources.
- Do not include personal details or identifiers that are not in the sources.
- If the sources do not answer the question, reply exactly: {refusal}
- Write 2 to 6 plain sentences. No headings, no lists, no preamble.

Question: {question}

Sources:
{sources}

Answer:""")
NARRATIVE_Q = re.compile(r"\b(charts?|epcrs?|narratives?|patients?|clinical)\b", re.I)
MONEY = re.compile(r"(revenue|cost|billed|amount|value_per|actual|planned|^variance$)", re.I)
HIDE_COLS = re.compile(r"^(prov_\w+|transport_id|claim_id|crew_id|shift_id)$")


@dataclass
class Answer:
    question: str
    role: str
    as_of: object
    kind: str
    answer: str
    refused: bool
    sources: list = field(default_factory=list)
    cited: list = field(default_factory=list)
    reason: str = None
    provider: str = "rules"
    search_mode: str = None
    search_notes: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    sql_rows: list = field(default_factory=list)
    attempts: int = 0
    ms: float = 0.0

    @property
    def citations(self):
        return [s["citation"] for s in self.sources if s["n"] in self.cited]

    def render(self):
        lines = [self.answer]
        if not self.refused and self.cited:
            lines.append("")
            lines.append("Sources:")
            for s in self.sources:
                if s["n"] in self.cited:
                    lines.append(f"[{s['n']}] [{', '.join(str(x) for x in s['citation'])}]")
        return "\n".join(lines)

    def to_dict(self):
        return {"question": self.question, "role": self.role, "as_of": str(self.as_of), "kind": self.kind,
                "answer": self.answer, "refused": self.refused, "reason": self.reason, "provider": self.provider,
                "citations": self.citations, "cited": self.cited,
                "sources": [{k: v for k, v in s.items() if k != "row"} for s in self.sources],
                "search_mode": self.search_mode, "search_notes": self.search_notes, "checks": self.checks,
                "attempts": self.attempts, "ms": round(self.ms, 1)}


# ------------------------------------------------------------ formatting
def fmt(col, v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat(sep=" ") if isinstance(v, dt.datetime) else v.isoformat()
    if isinstance(v, (int, float)) or type(v).__name__ == "Decimal":
        f = float(v)
        if MONEY.search(col) and not col.endswith("_pct"):
            return f"${f:,.0f}" if f == int(f) else f"${f:,.2f}"
        return str(int(f)) if f == int(f) else str(round(f, 4))
    return str(v).replace("[", "(").replace("]", ")")


def _key(c, row):
    return "; ".join(f"{k}={fmt(k, row.get(k))}" for k in c.key if k in row) or "row"


def build_sources(results, hits, as_of):
    srcs, n = [], 0
    for c, rows, total, _err in results:
        for r in rows:
            n += 1
            facts = "; ".join(f"{k} {fmt(k, v)}" for k, v in r.items() if not HIDE_COLS.match(k))
            srcs.append({"n": n, "kind": "sql", "view": c.view, "row": r,
                         "citation": [f"gold.{c.view}", _key(c, r), as_of.isoformat()], "text": facts})
        if total:
            n += 1
            facts = "; ".join(f"total {k} {fmt(k, v)}" for k, v in total.items())
            srcs.append({"n": n, "kind": "sql", "view": c.view, "row": total,
                         "citation": [f"gold.{c.view}", "total", as_of.isoformat()], "text": facts})
    for h in hits:
        n += 1
        srcs.append({"n": n, "kind": "doc", "citation": h.citation, "title": h.title, "text": h.text,
                     "chunk_id": h.chunk_id})
    return srcs


def source_block(sources):
    out = []
    for s in sources:
        if s["kind"] == "sql":
            out.append(f"[{s['n']}] governed metric {s['citation'][0]} ({s['citation'][1]}): {s['text']}")
        else:
            c = s["citation"]
            out.append(f"[{s['n']}] {s['title']}, {c[1]} (effective {c[2]}): {s['text'][:600]}")
    return "\n".join(out)


# ------------------------------------------------------------ rules writer
def _v(r, col):
    return fmt(col, r.get(col))


def rules_write(question, kind, sources):
    sql = [s for s in sources if s["kind"] == "sql"]
    docs = [s for s in sources if s["kind"] == "doc"]
    out = []
    if kind == "cross_department" and sql:
        s = sql[0]
        r, n = s["row"], s["n"]
        what = " ".join(x for x in (r.get("system"), r.get("issue_category")) if x)
        out.append(f"Tail {r.get('tail_number')} at {r.get('base_id')} was AOG for {_v(r, 'aog_hours')} hours "
                   f"in the month of {_v(r, 'month')}" + (f" ({what})" if what else "") + f" [{n}].")
        if "est_lost_transports" in r:
            out.append(f"It missed an estimated {_v(r, 'est_lost_transports')} transports, "
                       f"{_v(r, 'est_lost_revenue')} in lost revenue [{n}].")
        if "overtime_cost" in r:
            out.append(f"Crew overtime was {_v(r, 'overtime_hours')} hours, costing {_v(r, 'overtime_cost')} [{n}].")
        if "claims_at_risk_same_base_month" in r:
            out.append(f"In the same base and month, {_v(r, 'claims_at_risk_same_base_month')} claims were at risk of "
                       f"denial, {_v(r, 'billed_at_risk_same_base_month')} billed [{n}].")
        if "est_total_cost" in r:
            out.append(f"Estimated total cost (lost revenue plus overtime): {_v(r, 'est_total_cost')} [{n}].")
        for d in docs[:3]:
            out.append(f"{d['title']} {d['citation'][1]}: \"{d['text']}\" [{d['n']}].")
    elif sql:
        for s in sql:
            nums = [f"{k} {fmt(k, v)}" for k, v in s["row"].items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool) and not HIDE_COLS.match(k)]
            if nums:
                out.append(f"{s['view']} ({s['citation'][1]}): {', '.join(nums)} [{s['n']}].")
    elif docs:
        d = docs[0]
        c = d["citation"]
        out.append(f"{d['title']}, {c[1]} (effective {c[2]}): \"{d['text']}\" [{d['n']}].")
    return " ".join(out) if out else REFUSAL


# ------------------------------------------------------------ ask
def _open(db):
    import duckdb
    return duckdb.connect(str(db or REPO / "warehouse.duckdb"), read_only=True)


def gather(question, role, as_of=None, con=None, embedder=None, k=5, classify_provider="rules", model=None, **opts):
    """Steps 1-3 (no writing): kind, governed rows, passages, sources."""
    from search import search
    ents = PL.entities(question)
    as_of = PL.default_as_of(ents, as_of)
    kind, cls_by = PL.classify(question, classify_provider, model=model, **opts)
    results = PL.run_queries(con, role, PL.pick_queries(question, kind), ents)
    if kind != "document" and not any(rows for _, rows, _, _ in results) \
            and PL.rules_classify(question)["kind"] == "document":
        # the model saw a metric where the rules baseline sees a document question,
        # and no governed view answers it: take the rules baseline
        kind, cls_by, results = "document", f"rules (overrode {cls_by}: no governed rows)", []
    kinds = None if NARRATIVE_Q.search(question) else {"doc"}
    hits, seen, mode, notes = [], set(), None, []
    per_query = {"document": k, "metric": 2, "cross_department": 2}[kind]
    for i, sq in enumerate(PL.search_queries(question, kind)):
        res = search(con, sq, role=role, as_of=as_of, k=per_query if i == 0 else 1, embedder=embedder, kinds=kinds)
        mode = mode or res.mode
        notes += [x for x in res.notes if x not in notes]
        for h in res.hits:
            if h.chunk_id not in seen:
                seen.add(h.chunk_id)
                hits.append(h)
    hits = hits[:k]
    return {"as_of": as_of, "kind": kind, "classified_by": cls_by, "results": results, "hits": hits,
            "mode": mode, "notes": notes, "sources": build_sources(results, hits, as_of)}


def ask(question, role, as_of=None, provider="rules", con=None, db=None, embedder=None, k=5, model=None,
        classify_provider=None, **opts):
    t0 = time.perf_counter()
    own = con is None
    con = con or _open(db)
    try:
        g = gather(question, role, as_of, con, embedder, k, classify_provider or provider, model, **opts)
    finally:
        if own:
            con.close()
    sources, kind = g["sources"], g["kind"]
    sql_rows = [s["row"] for s in sources if s["kind"] == "sql"]
    a = Answer(question, role, g["as_of"], kind, REFUSAL, True, sources, provider=provider,
               search_mode=g["mode"], search_notes=g["notes"], sql_rows=sql_rows)
    a.checks["classified_by"] = g["classified_by"]
    errs = [e for _, _, _, e in g["results"] if e]
    if errs:
        a.checks["query_errors"] = errs
    if kind != "document" and not sql_rows:
        a.reason = "no governed metric rows for this role and question"
    elif not sources:
        a.reason = "no governed rows or ACTIVE passages in scope"
    else:
        _write(a, question, role, provider, model, opts)
    if not a.refused and role not in ("billing_phi",) and find_phi(a.answer):
        a.answer, a.refused, a.reason = REFUSAL, True, "identifier pattern in the answer"
    a.ms = (time.perf_counter() - t0) * 1000
    return a


def _write(a, question, role, provider, model, opts):
    version, tmpl = WRITE_PROMPT
    prompt = tmpl.format(role=role, refusal=REFUSAL, question=question, sources=source_block(a.sources))
    last = None
    for attempt in range(1 if provider == "rules" else 2):
        a.attempts = attempt + 1
        p = prompt if last is None else (prompt + "\n\nYour previous answer was rejected by the checker:\n- "
                                         + "\n- ".join(last["problems"][:8])
                                         + "\nRewrite it using only the sources' numbers and markers.\n\nAnswer:")
        try:
            text = llm.call(WRITE_TASK, p, provider=provider, model=model,
                            rules=lambda _p: rules_write(question, a.kind, a.sources), **opts)
        except Exception as e:
            a.reason = f"writer failed: {type(e).__name__}: {str(e)[:200]}"
            return
        text = (text or "").strip()
        if REFUSAL.lower().rstrip(".") in text.lower():
            a.reason = "the writer found the sources insufficient"
            return
        res = C.check(text, a.sources, a.sql_rows, question, a.as_of)
        a.checks["post_check"] = res
        if res["ok"]:
            a.answer, a.refused, a.cited, a.reason = text, False, res["cited"], None
            return
        last = res
    a.reason = "post-check failed: " + "; ".join(last["problems"][:5])
