"""Flight ops waves.

wave1 ops.classify    bronze.maintenance_events -> silver.mx_classified  router task wave1.ops.classify
wave1 ops.index       bronze.documents          -> silver.doc_sections   ACTIVE documents only, by section
wave2 ops.aog_impact  mx + dispatch + docs      -> silver.aog_impact     SQL
"""
import re

import llm
from waves import prompts
from waves.billing import policy_sections
from waves.framework import columns, fetch, gate, has_table, provenance, step
from waves.schemas import AogImpact, DocSection, MxClass, MxClassRow

CLASSIFY_TASK = "wave1.ops.classify"

SYSTEMS = (("hydraulic", r"hydraulic"), ("gearbox", r"gearbox|transmission|chip"), ("starting", r"starter|start(?:ing)? relay"),
           ("fuel", r"fuel"), ("avionics", r"avionics|radio|gps|display|autopilot|transponder"),
           ("electrical", r"electrical|generator|battery|bus\b|wiring"), ("engine", r"engine|turbine|compressor|oil pressure"),
           ("rotor", r"rotor|blade|swashplate"), ("landing gear", r"landing gear|tire|brake|skid"),
           ("airframe", r"airframe|door|windshield|structure|corrosion"))
CATEGORIES = (("pressure loss", r"pressure (?:decay|loss|drop)|decayed|lost pressure"), ("chip indication", r"chip"),
              ("relay failure", r"relay"), ("pump failure", r"pump .*fail|failed .*pressure test|pump failure"),
              ("leak", r"leak|seep"), ("crack", r"crack"), ("inspection", r"inspection|scheduled|phase|\d+-hour"),
              ("electrical fault", r"fault|short|tripped"), ("wear", r"worn|wear|chaf"))


def rule_classify(text, aog):
    t = text or ""
    system = next((s for s, p in SYSTEMS if re.search(p, t, re.I)), "other")
    cat = next((c for c, p in CATEGORIES if re.search(p, t, re.I)), "other")
    if aog or re.search(r"\bAOG\b|grounded|no[- ]go|out of service", t, re.I):
        sev = "AOG"
    elif re.search(r"defer|MEL|monitor", t, re.I):
        sev = "deferred"
    else:
        sev = "routine"
    return {"issue_category": cat, "system": system, "severity": sev}


@step("ops.classify", 1, "silver.mx_classified")
def classify_step(con, ctx, res):
    """Maintenance free text -> issue category, system, severity."""
    rows = fetch(con, "SELECT mx_id, tail_number, base_id, coalesce(aog_hours, 0) AS aog_hours, issue_text "
                      "FROM bronze.maintenance_events ORDER BY mx_id") if has_table(con, "bronze.maintenance_events") else []
    res.rows_in = len(rows)
    prov = res.provider = ctx.provider(CLASSIFY_TASK)
    version, tmpl = prompts.OPS_MX
    version = "ops_mx_rules.v1" if prov == "rules" else version
    model = ctx.model(prov)
    out = []
    for r in rows:
        aog = (r["aog_hours"] or 0) > 0
        p = provenance(r["mx_id"], 1, "ops.classify", prov, model, version)
        try:
            got = llm.call(CLASSIFY_TASK, r["issue_text"] if prov == "rules" else
                           tmpl.format(text=r["issue_text"], aog="yes" if aog else "no"), provider=prov,
                           schema=MxClass, model=model, rules=lambda _p, t=r["issue_text"], a=aog: rule_classify(t, a),
                           **ctx.llm_opts)
        except Exception as e:
            out.append(({"_reject": f"router: {type(e).__name__}: {str(e)[:300]}", "_payload": r}, p))
            continue
        out.append(({**got.model_dump(), "mx_id": r["mx_id"], "tail_number": r["tail_number"],
                     "base_id": r["base_id"]}, p))
    gate(con, "silver.mx_classified", MxClassRow, out, res, ctx.run_at)


@step("ops.index", 1, "silver.doc_sections")
def index_step(con, ctx, res):
    """ACTIVE documents split by section. SUPERSEDED versions never enter silver;
    callers filter effective_date <= the event date."""
    has_sections = has_table(con, "bronze.documents") and "sections" in columns(con, "bronze.documents")
    docs = fetch(con, f"""SELECT doc_id, upper(dept) AS dept, title, status, CAST(effective_date AS DATE) AS effective_date,
                                content {', sections' if has_sections else ''}
                          FROM bronze.documents ORDER BY doc_id""") if has_table(con, "bronze.documents") else []
    res.rows_in = len(docs)
    out = []
    for d in docs:
        if d["status"] != "ACTIVE":
            continue
        for sec, text in policy_sections(d):
            out.append(({"doc_id": d["doc_id"], "dept": d["dept"], "title": d["title"], "status": d["status"],
                         "effective_date": d["effective_date"], "section": sec, "text": text},
                        provenance(d["doc_id"], 1, "ops.index", "rules", "python", "index.v1")))
    res.notes.append(f"{sum(1 for d in docs if d['status'] != 'ACTIVE')} non-ACTIVE docs excluded")
    gate(con, "silver.doc_sections", DocSection, out, res, ctx.run_at)


LOST_VALUE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)[^.]{0,120}?missed", re.I)


def lost_transport_value(con):
    """(value, basis) from an ACTIVE finance document, else None."""
    if not has_table(con, "silver.doc_sections"):
        return None, None
    for r in fetch(con, "SELECT doc_id, section, effective_date, text FROM silver.doc_sections "
                        "WHERE dept = 'FINANCE' ORDER BY effective_date DESC, doc_id"):
        m = LOST_VALUE.search(r["text"])
        if m:
            return float(m.group(1).replace(",", "")), f"[{r['doc_id']}, {r['section']}, {r['effective_date']}]"
    return None, None


@step("ops.aog_impact", 2, "silver.aog_impact")
def aog_impact_step(con, ctx, res):
    """AOG events -> declined requests -> estimated lost transports and revenue (SQL)."""
    if not has_table(con, "bronze.maintenance_events"):
        return gate(con, "silver.aog_impact", AogImpact, [], res, ctx.run_at)
    dcols = columns(con, "bronze.dispatch_log") if has_table(con, "bronze.dispatch_log") else []
    if not dcols:
        match = "FALSE"
    elif "mx_id" in dcols:
        match = "d.mx_id = m.mx_id"
    else:
        tail = " AND d.tail_number = m.tail_number" if "tail_number" in dcols else ""
        match = f'd.base_id = m.base_id{tail} AND d.ts >= m."start" AND d.ts < coalesce(m."end", m."start" + INTERVAL 1 DAY)'
    dispatch = "bronze.dispatch_log" if dcols else "(SELECT NULL AS outcome) "
    value, basis = lost_transport_value(con)
    base_avg = ("(SELECT base_id, avg(collected_amount) AS v FROM bronze.transports WHERE collected_amount > 0 GROUP BY base_id)"
                if has_table(con, "bronze.transports") else "(SELECT NULL AS base_id, NULL AS v)")
    rows = fetch(con, f"""
        WITH declined AS (
            SELECT m.mx_id, count(d.outcome) FILTER (WHERE d.outcome = 'declined_aog') AS n
            FROM bronze.maintenance_events m LEFT JOIN {dispatch} d ON {match}
            GROUP BY m.mx_id)
        SELECT m.mx_id, m.tail_number, m.base_id, m."start" AS start_ts, m."end" AS end_ts,
               coalesce(m.aog_hours, 0) AS aog_hours, coalesce(m.missed_requests, 0) AS missed_requests,
               coalesce(x.n, 0) AS declined_aog_logged,
               CASE WHEN coalesce(x.n, 0) > 0 THEN x.n ELSE coalesce(m.missed_requests, 0) END AS est_lost_transports,
               b.v AS base_avg
        FROM bronze.maintenance_events m
        LEFT JOIN declined x USING (mx_id)
        LEFT JOIN {base_avg} b ON b.base_id = m.base_id
        WHERE coalesce(m.aog_hours, 0) > 0
        ORDER BY m.mx_id""")
    res.rows_in, res.provider = len(rows), "sql"
    out = []
    for r in rows:
        v, why = (value, f"finance policy {basis}") if value is not None else \
            (round(r["base_avg"], 2) if r["base_avg"] is not None else None, "avg collected per paid transport at base")
        r.pop("base_avg")
        out.append(({**r, "value_per_lost_transport": v, "value_basis": why,
                     "est_lost_revenue": None if v is None else round(v * r["est_lost_transports"], 2)},
                    provenance(r["mx_id"], 2, "ops.aog_impact", "sql", "sql", "aog_impact.v1")))
    gate(con, "silver.aog_impact", AogImpact, out, res, ctx.run_at)


STEPS = [classify_step, index_step, aog_impact_step]
