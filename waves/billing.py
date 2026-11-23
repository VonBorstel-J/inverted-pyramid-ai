"""Billing / RCM waves.

wave1 billing.redact     bronze.epcrs        -> silver.epcr_deid       rules + model names (local/rules only)
wave1 billing.extract    silver.epcr_deid    -> silver.epcr_extract    router task wave1.billing.extract
wave2 billing.reconcile  claims+extract+docs -> silver.claim_reconciliation  deterministic
"""
import datetime as dt
import json
import re

import llm
from llm.phi import PHI_SAFE_PROVIDERS, find_phi
from waves import prompts, redact as R
from waves.framework import fetch, gate, has_table, provenance, step
from waves.schemas import ClaimReconciliation, EpcrDeid, EpcrExtract, EpcrExtractRow, NameList

NAMES_TASK, EXTRACT_TASK = "wave1.billing.redact_names", "wave1.billing.extract"


# ------------------------------------------------------------ wave 1: redaction
@step("billing.redact", 1, "silver.epcr_deid")
def redact_step(con, ctx, res):
    """Rules-first Safe Harbor redaction; the model only adds free-text names."""
    rows = fetch(con, "SELECT doc_id, transport_id, raw_text FROM bronze.epcrs ORDER BY doc_id") \
        if has_table(con, "bronze.epcrs") else []
    res.rows_in = len(rows)
    prov = ctx.provider(NAMES_TASK)
    if prov not in PHI_SAFE_PROVIDERS:
        res.notes.append(f"{NAMES_TASK}: {prov} may not see raw PHI, using rules")
        prov = "rules"
    res.provider = prov
    version, tmpl = prompts.BILLING_NAMES
    model = ctx.model(prov)
    fallbacks = 0
    out = []
    for r in rows:
        text = r["raw_text"] or ""
        used = prov
        try:
            got = llm.call(NAMES_TASK, tmpl.format(text=text) if prov != "rules" else text, provider=prov,
                           schema=NameList, model=model,
                           rules=lambda _p, t=text: {"names": R.rule_names(t)}, **ctx.llm_opts)
            names = [n for n in got.names if n and n in text]
        except Exception:
            fallbacks += 1
            used, names = "rules", []
        deid, counts = R.redact(text, names)
        p = provenance(r["doc_id"], 1, "billing.redact", used, model if used != "rules" else "regex",
                       version if used != "rules" else "redact_rules.v1")
        leaked = find_phi(deid)
        if leaked:  # gate: never write text the PHI guard would refuse
            out.append(({"_reject": f"identifier pattern left after redaction: {','.join(leaked)}",
                         "_payload": {"doc_id": r["doc_id"]}}, p))
            continue
        out.append(({"doc_id": r["doc_id"], "transport_id": r["transport_id"], "deid_text": deid,
                     "n_redactions": sum(counts.values()), "redaction_types": sorted(counts)}, p))
    if fallbacks:
        res.notes.append(f"{fallbacks} docs fell back to rule names after a model error")
    gate(con, "silver.epcr_deid", EpcrDeid, out, res, ctx.run_at)


# ------------------------------------------------------------ wave 1: extraction
UNSTABLE = re.compile(r"\b(VT|VF|GCS|SpO2|sat(?:uration)?s?|MAP|BP|hypotens\w*|pressors?|intubat\w*|unstable|desat\w*|"
                      r"brady\w*|tachy\w*|seiz\w*|apnea|shock|lactate)\b", re.I)
GROUND = re.compile(r"\b(ground|road|ambulance|ALS unit|BLS unit|highway|drive time|by land|surface)\b", re.I)
UNSAFE = re.compile(r"\b(unsafe|contraindicat\w*|could not|couldn't|cannot|can't|unable|lacked|lacks|ruled out|"
                    r"exceed\w*|no ground crew|not tolerate|not feasible|too long|would delay|unavailable|"
                    r"not available|precluded|not an option)\b", re.I)
CERT_NO = re.compile(r"\b(no physician signature|pending|not signed|unsigned|missing (?:physician )?(?:signature|certification)|"
                     r"no (?:physician )?certification|awaiting (?:signature|certification))\b", re.I)
CERT_YES = re.compile(r"\b(certification signed|signed (?:the )?(?:physician )?certification|attested|certified|"
                      r"PCS signed|physician signed|MD signed|signature (?:on file|obtained))\b", re.I)
DX = re.compile(r"(?:working dx|dx|diagnosis|impression|handoff lists)\s*[:—–-]?\s*([^.\n]+)", re.I)
MILES = re.compile(r"\b(\d{1,4})(?:\.\d+)?\s*(?:loaded\s+)?(?:mi|miles|statute miles|nm)\b|loaded (?:miles|mileage)\s*[:=]?\s*(\d{1,4})", re.I)


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def rule_extract(text):
    """Deterministic baseline for wave1.billing.extract."""
    out = {"diagnosis": None, "instability_evidence": None, "ground_contraindication": None,
           "physician_certification": False, "loaded_miles": None}
    m = DX.search(text or "")
    if m:
        out["diagnosis"] = m.group(1).strip()
    for s in sentences(text):
        core = s.rstrip(".")
        if out["ground_contraindication"] is None and GROUND.search(s) and UNSAFE.search(s):
            out["ground_contraindication"] = s
        elif out["instability_evidence"] is None and UNSTABLE.search(s) and not DX.search(s):
            out["instability_evidence"] = re.sub(r"^(recheck|on loading|per sending rn)\s*[:,]?\s*", "", core, flags=re.I)
    if CERT_NO.search(text or ""):
        out["physician_certification"] = False
    elif CERT_YES.search(text or ""):
        out["physician_certification"] = True
    m = MILES.search(text or "")
    if m:
        out["loaded_miles"] = int(m.group(1) or m.group(2))
    return out


@step("billing.extract", 1, "silver.epcr_extract")
def extract_step(con, ctx, res):
    """Structured fields from de-identified charts (model task, rules baseline)."""
    rows = fetch(con, "SELECT doc_id, transport_id, deid_text FROM silver.epcr_deid ORDER BY doc_id") \
        if has_table(con, "silver.epcr_deid") else []
    res.rows_in = len(rows)
    prov = res.provider = ctx.provider(EXTRACT_TASK)
    version, tmpl = prompts.BILLING_EXTRACT
    if prov == "rules":
        version = "billing_extract_rules.v1"
    model = ctx.model(prov)
    out = []
    for r in rows:
        text = r["deid_text"]
        p = provenance(r["doc_id"], 1, "billing.extract", prov, model, version)
        try:
            got = llm.call(EXTRACT_TASK, text if prov == "rules" else tmpl.format(text=text), provider=prov,
                           schema=EpcrExtract, model=model, rules=lambda _p, t=text: rule_extract(t), **ctx.llm_opts)
        except Exception as e:
            out.append(({"_reject": f"router: {type(e).__name__}: {str(e)[:300]}", "_payload": {"doc_id": r["doc_id"]}}, p))
            continue
        d = got.model_dump()
        span = d.get("ground_contraindication")
        if span is not None and span.strip() not in text:
            out.append(({"_reject": "ground_contraindication is not a verbatim span of the chart",
                         "_payload": d}, p))
            continue
        out.append(({**d, "doc_id": r["doc_id"], "transport_id": r["transport_id"]}, p))
    gate(con, "silver.epcr_extract", EpcrExtractRow, out, res, ctx.run_at)


# ------------------------------------------------------------ wave 2: reconciliation
SECTION_LINE = re.compile(r"^\s*((?:Sec(?:tion)?\.?|§)\s*[\d.]+[a-z]?|\d+(?:\.\d+)+)\s*[:.)-]?\s+(.*)$", re.I)
REQUIREMENTS = (  # (requirement, pattern on the policy section text, extracted field)
    ("ground_contraindication", re.compile(r"ground|road|ambulance|surface transport", re.I)),
    ("physician_certification", re.compile(r"physician certification|certif|physician sign", re.I)),
    ("instability_evidence", re.compile(r"instab|unstable|vital signs", re.I)),
)
MAX_MILES = re.compile(r"(?:not exceed|no more than|maximum of|max(?:imum)?|up to|cap(?:ped)? at)\s*(\d{1,4})\s*(?:loaded\s+)?miles", re.I)
REASON_TEXT = {"ground_contraindication": "missing ground contraindication",
               "physician_certification": "missing certification",
               "instability_evidence": "missing instability evidence",
               "max_miles": "loaded miles over policy cap"}


def policy_sections(doc):
    """[(section, text)] from the documents.sections JSON when present, else
    from 'Sec 4.2 ...' style lines in content."""
    raw = doc.get("sections")
    if raw:
        try:
            d = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(d, dict) and d:
                return [(str(k), str(v)) for k, v in d.items()]
        except (TypeError, ValueError):
            pass
    out, cur = [], None
    for line in (doc.get("content") or "").splitlines():
        m = SECTION_LINE.match(line)
        if m:
            cur = [m.group(1).strip(), m.group(2).strip()]
            out.append(cur)
        elif cur and line.strip():
            cur[1] += " " + line.strip()
    return [tuple(x) for x in out] or [("1", doc.get("content") or "")]


def policy_requirements(doc):
    """[(requirement, section, threshold)] read from the policy text."""
    reqs = []
    for sec, text in policy_sections(doc):
        for name, pat in REQUIREMENTS:
            if pat.search(text) and name not in [r[0] for r in reqs]:
                reqs.append((name, sec, None))
        m = MAX_MILES.search(text)
        if m:
            reqs.append(("max_miles", sec, int(m.group(1))))
    return reqs


def policy_in_effect(policies, payer, on_date):
    """The ACTIVE policy for this payer with the latest effective_date on or
    before the date. SUPERSEDED (or any non-ACTIVE) documents are never used."""
    best = None
    for d in policies:
        if d.get("status") != "ACTIVE":
            continue
        if not (d.get("payer") == payer or (payer and payer.lower() in (d.get("title") or "").lower())):
            continue
        eff = d.get("effective_date")
        if eff is None or eff > on_date:
            continue
        if best is None or (eff, d["doc_id"]) > (best["effective_date"], best["doc_id"]):
            best = d
    return best


def check_claim(extract, policy):
    """Deterministic match of extracted fields to the policy requirements.
    Returns (failed [(requirement, section)], first_section)."""
    failed = []
    for name, sec, threshold in policy_requirements(policy):
        if name == "max_miles":
            miles = (extract or {}).get("loaded_miles")
            if miles is not None and miles > threshold:
                failed.append((name, sec))
        elif name == "physician_certification":
            if not (extract or {}).get("physician_certification"):
                failed.append((name, sec))
        elif not (extract or {}).get(name):
            failed.append((name, sec))
    return failed


@step("billing.reconcile", 2, "silver.claim_reconciliation")
def reconcile_step(con, ctx, res):
    """Each claim vs the payer policy in effect on the transport date."""
    if not all(has_table(con, t) for t in ("bronze.claims", "bronze.transports")):
        res.notes.append("bronze claims/transports missing")
        return gate(con, "silver.claim_reconciliation", ClaimReconciliation, [], res, ctx.run_at)
    claims = fetch(con, """SELECT c.claim_id, c.transport_id, coalesce(c.payer, t.payer) AS payer,
                                  CAST(t.date AS DATE) AS transport_date
                           FROM bronze.claims c LEFT JOIN bronze.transports t USING (transport_id)
                           ORDER BY c.claim_id""")
    doc_cols = [c for c in ("sections", "payer") if c in _doc_columns(con)]
    policies = fetch(con, f"""SELECT doc_id, title, status, CAST(effective_date AS DATE) AS effective_date, content
                              {''.join(', ' + c for c in doc_cols)}
                              FROM bronze.documents WHERE upper(dept) = 'BILLING'""") \
        if has_table(con, "bronze.documents") else []
    extracts = {r["transport_id"]: r for r in fetch(con, "SELECT * FROM silver.epcr_extract")} \
        if has_table(con, "silver.epcr_extract") else {}
    res.rows_in, res.provider = len(claims), "rules"
    out = []
    for c in claims:
        p = provenance(f"{c['claim_id']},{c['transport_id']}", 2, "billing.reconcile", "rules", "python",
                       "reconcile.v1")
        base = {"claim_id": c["claim_id"], "transport_id": c["transport_id"], "payer": c["payer"],
                "transport_date": c["transport_date"]}
        if c["transport_date"] is None:
            out.append(({"_reject": "claim has no transport date", "_payload": base}, p))
            continue
        pol = policy_in_effect(policies, c["payer"], c["transport_date"])
        if pol is None:
            out.append(({**base, "at_risk": True, "reason": "no payer policy in effect on the transport date",
                         "failed_requirements": ["policy"]}, p))
            continue
        ex = extracts.get(c["transport_id"])
        if ex is None:
            failed = [("epcr", None)]
            reason = "no extracted ePCR for the transport"
            sec = policy_sections(pol)[0][0]
        else:
            failed = check_claim(ex, pol)
            reason = "; ".join(REASON_TEXT[f] for f, _ in failed) or "meets policy"
            sec = failed[0][1] if failed else ", ".join(s for s, _ in policy_sections(pol))
        out.append(({**base, "at_risk": bool(failed), "reason": reason, "policy_doc_id": pol["doc_id"],
                     "policy_section": sec, "policy_effective_date": pol["effective_date"],
                     "citation": [pol["doc_id"], sec, pol["effective_date"].isoformat()],
                     "failed_requirements": [f for f, _ in failed]}, p))
    gate(con, "silver.claim_reconciliation", ClaimReconciliation, out, res, ctx.run_at)


def _doc_columns(con):
    from waves.framework import columns
    return columns(con, "bronze.documents") if has_table(con, "bronze.documents") else []


STEPS = [redact_step, extract_step, reconcile_step]
