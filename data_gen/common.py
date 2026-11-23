"""Shared identities, calendar and ordered bronze schemas. Monetary units are USD."""
from collections import defaultdict
from datetime import date
from random import Random

START = date(2026, 6, 1)
END = date(2026, 10, 31)
DAYS = (END - START).days + 1
BASES = ("DEN-01", "RIM-02", "FEN-03", "ARC-04", "ASH-05", "BAY-06", "ELM-07", "TOR-08")
FLEET = dict(zip(BASES, (("N412AM", "N621QZ"), ("N107QX",), ("N218XZ", "N229XZ"),
                          ("N331QZ",), ("N446XZ", "N452QX"), ("N564QZ",),
                          ("N678XZ", "N684QZ"), ("N791QX",))))
MONTHS = tuple(date(2026, m, 1) for m in range(6, 11))


def key(prefix, n):
    return f"{prefix}-2026-{n:06d}"


def month(day):
    return day.replace(day=1)


class Dataset:
    def __init__(self, seed):
        self.rng = Random(seed)
        self.tables = defaultdict(list)

    def add(self, table, *values):
        self.tables[table].append(dict(zip(SCHEMAS[table], values, strict=True)))

    def document(self, doc_id, dept, title, status, effective, sections):
        import json
        content = "\n".join(f"{label} {body}" for label, body in sections.items())
        self.add("documents", doc_id, dept, title, status, effective,
                 json.dumps(sections, ensure_ascii=False), content)


# A single column order for row construction and typed DuckDB materialization.
def schema(spec):
    return dict(field.split(":") if ":" in field else (field, "VARCHAR") for field in spec.split())


SCHEMAS = {name: schema(fields) for name, fields in {
    "aircraft": "tail_number base_id type in_service:BOOLEAN",
    "dispatch_log": "request_id base_id ts:TIMESTAMP outcome transport_id tail_number mx_id",
    "maintenance_events": "mx_id tail_number base_id start:TIMESTAMP end:TIMESTAMP aog_hours:INTEGER issue_text missed_requests:INTEGER",
    "weather_declines": "request_id base_id ts:TIMESTAMP reason_text",
    "transports": "transport_id base_id tail_number date:DATE payer cpt_code loaded_miles:INTEGER icd10_code billed_amount:INTEGER collected_amount:INTEGER claim_status",
    "epcrs": "doc_id transport_id raw_text",
    "claims": "claim_id transport_id payer submitted_date:DATE status denial_code paid_amount:INTEGER",
    "documents": "doc_id dept title status effective_date:DATE sections:JSON content",
    "crew": "crew_id base_id role hire_date:DATE",
    "licenses": "crew_id kind expires:DATE",
    "shifts": "shift_id crew_id date:DATE hours:INTEGER overtime_hours:INTEGER reason_text mx_id flown:BOOLEAN",
    "leases": "tail_number monthly_cost:INTEGER",
    "base_ledger": "base_id month:DATE revenue:INTEGER fuel:INTEGER maintenance_cost:INTEGER crew_cost:INTEGER lease_cost:INTEGER",
    "budget": "base_id month:DATE line planned:INTEGER",
    "truth_epcrs": "doc_id transport_id diagnosis instability_evidence physician_certification:BOOLEAN loaded_miles:INTEGER has_contraindication:BOOLEAN contraindication span_start:INTEGER span_end:INTEGER",
    "truth_phi": "doc_id phi_type value span_start:INTEGER span_end:INTEGER",
    "truth_claim_risk": "claim_id at_risk:BOOLEAN reason doc_id section",
    "truth_maintenance": "mx_id issue_category system severity",
    "truth_aog_impacts": "mx_id request_id base_id tail_number month:DATE lost_transports:INTEGER lost_revenue:INTEGER",
    "truth_overtime": "shift_id mx_id overtime_hours:INTEGER overtime_cost:INTEGER",
    "truth_shift_flags": "shift_id duty_limit_breach:BOOLEAN expired_on_flown_shift:BOOLEAN",
    "truth_license_expiries": "crew_id kind expires:DATE",
    "truth_ledger_variances": "base_id month:DATE line planned:INTEGER actual:INTEGER variance:INTEGER driver",
    "truth_sop_traps": "question retired_question active_doc_id superseded_doc_id section active_answer superseded_answer retired_question_has_active_answer:BOOLEAN",
}.items()}
