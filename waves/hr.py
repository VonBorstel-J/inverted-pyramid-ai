"""HR / crew waves.

wave1 hr.licenses       bronze.licenses -> silver.license_status   SQL
wave1 hr.overtime       bronze.shifts   -> silver.overtime_causes  router task wave1.hr.overtime_cause
wave2 hr.compliance     shifts+licenses+handbook -> silver.hr_compliance_flags  SQL
wave2 hr.aog_overtime   overtime_causes + mx     -> silver.aog_overtime        SQL
"""
import datetime as dt
import re

import llm
from waves import prompts
from waves.framework import columns, fetch, gate, has_table, provenance, step
from waves.schemas import AogOvertime, ComplianceFlag, LicenseStatus, OvertimeCause, OvertimeCauseRow

OT_TASK = "wave1.hr.overtime_cause"
TAIL = re.compile(r"\bN\d{3}[A-Z]{2}\b")
CAUSES = (("aog", r"\bAOG\b|aircraft (?:down|on ground)|grounded|maintenance|\bmx\b|recovery cover|out of service"),
          ("weather", r"weather|IFR|fog|icing|ceiling|storm|wind"),
          ("staffing", r"call[- ]?out|sick|short[- ]staffed|vacan|coverage|cover for|no relief|holdover"),
          ("training", r"training|check ?ride|recurrent|simulator|class"),
          ("mission", r"late (?:call|mission|transport)|extended (?:mission|transport)|long transport|late return"),
          ("scheduled", r"scheduled duty|scheduled shift|planned"))


def rule_cause(text):
    t = text or ""
    cause = next((c for c, p in CAUSES if re.search(p, t, re.I)), "other")
    m = TAIL.search(t)
    return {"cause": cause, "tail_number": m.group(0) if m else None,
            "mentions_aog": bool(re.search(CAUSES[0][1], t, re.I))}


def _as_of(con, ctx):
    if ctx.as_of:
        return ctx.as_of
    if has_table(con, "bronze.shifts"):
        d = con.execute("SELECT max(CAST(date AS DATE)) FROM bronze.shifts").fetchone()[0]
        if d:
            return d
    return dt.date.today()


def _shift_key(con):
    return "shift_id" if "shift_id" in columns(con, "bronze.shifts") else "crew_id || '@' || CAST(date AS VARCHAR)"


@step("hr.licenses", 1, "silver.license_status")
def licenses_step(con, ctx, res):
    """Expiry status of each crew license as of the window end."""
    if not has_table(con, "bronze.licenses"):
        return gate(con, "silver.license_status", LicenseStatus, [], res, ctx.run_at)
    as_of = _as_of(con, ctx)
    start = con.execute("SELECT min(CAST(date AS DATE)) FROM bronze.shifts").fetchone()[0] \
        if has_table(con, "bronze.shifts") else None
    rows = fetch(con, """
        SELECT crew_id, kind, CAST(expires AS DATE) AS expires,
               CASE WHEN CAST(expires AS DATE) < ? THEN 'expired'
                    WHEN CAST(expires AS DATE) <= ? + INTERVAL 30 DAY THEN 'expiring_30d' ELSE 'valid' END AS status,
               CAST(expires AS DATE) BETWEEN coalesce(?, DATE '1900-01-01') AND ? AS expires_in_window,
               CAST(? AS DATE) AS as_of
        FROM bronze.licenses ORDER BY crew_id, kind""", [as_of, as_of, start, as_of, as_of])
    res.rows_in, res.provider = len(rows), "sql"
    gate(con, "silver.license_status", LicenseStatus,
         [(r, provenance(f"{r['crew_id']},{r['kind']}", 1, "hr.licenses", "sql", "sql", "licenses.v1")) for r in rows],
         res, ctx.run_at)


@step("hr.overtime", 1, "silver.overtime_causes")
def overtime_step(con, ctx, res):
    """Overtime cause from reason_text (model task, rules baseline); links to
    the AOG event deterministically (tail + date inside the outage)."""
    if not has_table(con, "bronze.shifts"):
        return gate(con, "silver.overtime_causes", OvertimeCauseRow, [], res, ctx.run_at)
    rows = fetch(con, f"""SELECT {_shift_key(con)} AS shift_id, s.crew_id, CAST(s.date AS DATE) AS date,
                                 s.overtime_hours, s.reason_text, c.base_id
                          FROM bronze.shifts s LEFT JOIN {'bronze.crew' if has_table(con, 'bronze.crew') else '(SELECT NULL AS crew_id, NULL AS base_id)'} c
                            USING (crew_id)
                          WHERE coalesce(s.overtime_hours, 0) > 0 ORDER BY 1""")
    mx = fetch(con, """SELECT mx_id, tail_number, base_id, CAST("start" AS DATE) AS d0,
                              CAST(coalesce("end", "start") AS DATE) AS d1
                       FROM bronze.maintenance_events WHERE coalesce(aog_hours, 0) > 0""") \
        if has_table(con, "bronze.maintenance_events") else []
    res.rows_in = len(rows)
    prov = res.provider = ctx.provider(OT_TASK)
    version, tmpl = prompts.HR_OVERTIME
    version = "hr_overtime_rules.v1" if prov == "rules" else version
    model = ctx.model(prov)
    out = []
    for r in rows:
        p = provenance(r["shift_id"], 1, "hr.overtime", prov, model, version)
        text = r["reason_text"] or ""
        try:
            got = llm.call(OT_TASK, text if prov == "rules" else tmpl.format(text=text), provider=prov,
                           schema=OvertimeCause, model=model, rules=lambda _p, t=text: rule_cause(t), **ctx.llm_opts)
        except Exception as e:
            out.append(({"_reject": f"router: {type(e).__name__}: {str(e)[:300]}", "_payload": r}, p))
            continue
        g = got.model_dump()
        if g["tail_number"] and g["tail_number"] not in text:
            g["tail_number"] = None  # never keep a tail the text does not name
        link = None
        if g["cause"] == "aog" or g["mentions_aog"]:
            for e in mx:
                same = (e["tail_number"] == g["tail_number"]) if g["tail_number"] else (e["base_id"] == r["base_id"])
                if same and e["d0"] <= r["date"] <= e["d1"]:
                    link = e["mx_id"]
                    break
        out.append(({**g, "shift_id": r["shift_id"], "crew_id": r["crew_id"], "date": r["date"],
                     "overtime_hours": r["overtime_hours"], "mx_id": link}, p))
    gate(con, "silver.overtime_causes", OvertimeCauseRow, out, res, ctx.run_at)


DUTY = re.compile(r"(?:not exceed|no more than|maximum of|max(?:imum)?|limit(?:ed)? (?:to|of))\s*(\d{1,2})\s*hours", re.I)
OT_RATE = re.compile(r"overtime at \$\s?(\d+(?:\.\d+)?) per hour|\$\s?(\d+(?:\.\d+)?) per (?:overtime )?hour[^.]*overtime", re.I)
DEFAULT_DUTY_LIMIT = 14


def _doc_rule(con, dept, pattern):
    """(value, citation) for the first ACTIVE section in dept matching pattern."""
    if not has_table(con, "silver.doc_sections"):
        return None, None
    for r in fetch(con, "SELECT doc_id, section, effective_date, text FROM silver.doc_sections WHERE dept = ? "
                        "ORDER BY effective_date DESC, doc_id, section", [dept]):
        m = pattern.search(r["text"])
        if m:
            return float(next(g for g in m.groups() if g)), [r["doc_id"], r["section"], str(r["effective_date"])]
    return None, None


@step("hr.compliance", 2, "silver.hr_compliance_flags")
def compliance_step(con, ctx, res):
    """Expired license on a flown shift; duty-limit breach (limit read from the handbook)."""
    if not all(has_table(con, t) for t in ("bronze.shifts", "bronze.crew")):
        return gate(con, "silver.hr_compliance_flags", ComplianceFlag, [], res, ctx.run_at)
    limit, cite = _doc_rule(con, "HR", DUTY)
    if limit is None:
        limit, cite = DEFAULT_DUTY_LIMIT, None
        res.notes.append(f"no duty limit in an ACTIVE HR doc, default {DEFAULT_DUTY_LIMIT}h")
    lic_cite = None
    if has_table(con, "silver.doc_sections"):
        hit = fetch(con, "SELECT doc_id, section, effective_date FROM silver.doc_sections WHERE dept='HR' AND "
                         "regexp_matches(lower(text), 'expir|credential|licen') ORDER BY doc_id LIMIT 1")
        lic_cite = [hit[0]["doc_id"], hit[0]["section"], str(hit[0]["effective_date"])] if hit else None
    scol = columns(con, "bronze.shifts")
    if "flown" in scol:
        flown = "coalesce(s.flown, FALSE)"
    elif has_table(con, "bronze.dispatch_log"):
        flown = ("(c.role <> 'mechanic' AND EXISTS (SELECT 1 FROM bronze.dispatch_log d WHERE d.base_id = c.base_id "
                 "AND d.outcome = 'accepted' AND CAST(d.ts AS DATE) = CAST(s.date AS DATE)))")
    else:
        flown = "(c.role <> 'mechanic')"
    key = _shift_key(con).replace("crew_id", "s.crew_id").replace("CAST(date", "CAST(s.date") \
        if "shift_id" not in scol else "s.shift_id"
    lic = "bronze.licenses" if has_table(con, "bronze.licenses") else "(SELECT NULL AS crew_id, NULL AS kind, NULL::DATE AS expires)"
    rows = fetch(con, f"""
        WITH s AS (SELECT {key} AS shift_id, s.crew_id, c.base_id, CAST(s.date AS DATE) AS date, s.hours,
                          {flown} AS flown
                   FROM bronze.shifts s JOIN bronze.crew c USING (crew_id))
        SELECT s.shift_id, s.crew_id, s.base_id, s.date, 'expired_license_on_flown_shift' AS flag,
               'flew on ' || CAST(s.date AS VARCHAR) || ' with ' || l.kind || ' expired ' || CAST(CAST(l.expires AS DATE) AS VARCHAR) AS detail, 'lic' AS c
        FROM s JOIN {lic} l USING (crew_id)
        WHERE s.flown AND s.date > CAST(l.expires AS DATE)
        UNION ALL
        SELECT s.shift_id, s.crew_id, s.base_id, s.date, 'duty_limit_breach',
               CAST(s.hours AS VARCHAR) || 'h shift over the {int(limit)}h duty limit', 'duty'
        FROM s WHERE s.hours > {limit}
        ORDER BY 1, 5""")
    res.rows_in, res.provider = len(rows), "sql"
    out = []
    for r in rows:
        c = r.pop("c")
        r["citation"] = lic_cite if c == "lic" else cite
        out.append((r, provenance(r["shift_id"], 2, "hr.compliance", "sql", "sql", "compliance.v1")))
    gate(con, "silver.hr_compliance_flags", ComplianceFlag, out, res, ctx.run_at)


@step("hr.aog_overtime", 2, "silver.aog_overtime")
def aog_overtime_step(con, ctx, res):
    """Overtime that an AOG caused, costed at the rate in the finance policy (SQL)."""
    if not has_table(con, "silver.overtime_causes"):
        return gate(con, "silver.aog_overtime", AogOvertime, [], res, ctx.run_at)
    rate, cite = _doc_rule(con, "FINANCE", OT_RATE)
    if rate is None:
        res.notes.append("no overtime rate in an ACTIVE finance doc: cost left null")
    else:
        res.notes.append(f"rate {rate} from {cite}")
    crew = "bronze.crew" if has_table(con, "bronze.crew") else "(SELECT NULL AS crew_id, NULL AS base_id)"
    mx = "bronze.maintenance_events" if has_table(con, "bronze.maintenance_events") else "(SELECT NULL AS mx_id, NULL AS tail_number)"
    rows = fetch(con, f"""
        SELECT o.shift_id, o.crew_id, c.base_id, o.date, o.mx_id, m.tail_number, o.overtime_hours,
               CAST(? AS DOUBLE) AS ot_rate, round(o.overtime_hours * CAST(? AS DOUBLE), 2) AS overtime_cost
        FROM silver.overtime_causes o LEFT JOIN {crew} c USING (crew_id) LEFT JOIN {mx} m USING (mx_id)
        WHERE o.mx_id IS NOT NULL ORDER BY o.shift_id""", [rate, rate])
    res.rows_in, res.provider = len(rows), "sql"
    gate(con, "silver.aog_overtime", AogOvertime,
         [(r, provenance(f"{r['shift_id']},{r['mx_id']}", 2, "hr.aog_overtime", "sql", "sql", "aog_overtime.v1"))
          for r in rows], res, ctx.run_at)


STEPS_W1 = [licenses_step, overtime_step]
STEPS_W2 = [compliance_step, aog_overtime_step]
