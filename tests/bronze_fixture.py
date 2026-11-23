"""A tiny bronze warehouse that follows ARCHITECTURE.md section 3's table
shapes. Independent of data_gen. All names, numbers and identifiers are fake."""
import datetime as dt
import re

import duckdb

D = dt.date
TS = dt.datetime

# planted PHI: (doc_id, kind, value)
PHI = [
    ("EP-1", "name", "Drelwick, Talviq"), ("EP-1", "dob", "12 Apr 1961"), ("EP-1", "ssn", "901-00-0001"),
    ("EP-1", "mrn", "QV/000101"), ("EP-1", "phone", "(202) 555-0101"), ("EP-1", "address", "11 Quillglass Spur"),
    ("EP-2", "name", "Embril Q. Vossquill"), ("EP-2", "dob", "1958-02-03"), ("EP-2", "ssn", "902-00-0002"),
    ("EP-2", "mrn", "MR-0002-X"), ("EP-2", "phone", "202-555-0102"), ("EP-2", "address", "12 Vexmere Ct"),
    ("EP-3", "name", "Orvessa Fernivex"), ("EP-3", "dob", "07/30/1944"), ("EP-3", "ssn", "903-00-0003"),
    ("EP-3", "mrn", "ZQ0000103"), ("EP-3", "phone", "202.555.0103"), ("EP-3", "address", "13 Drelfen Rise"),
    ("EP-4", "name", "Tarnivell, Nimvian"), ("EP-4", "dob", "Mar 9, 1970"), ("EP-4", "ssn", "904-00-0004"),
    ("EP-4", "mrn", "QV/000104"), ("EP-4", "phone", "(202) 555-0104"), ("EP-4", "address", "14 Quillglass Spur"),
    ("EP-5", "name", "Zelquinn Quenvoss"), ("EP-5", "dob", "1931-11-11"), ("EP-5", "ssn", "905-00-0005"),
    ("EP-5", "mrn", "MR-0005-X"), ("EP-5", "phone", "202-555-0105"), ("EP-5", "address", "15 Vexmere Ct"),
    ("EP-5", "email", "z.quenvoss@example.org"), ("EP-5", "plan_id", "QB-55120-7"), ("EP-5", "account", "7781-2210"),
    ("EP-5", "age", "94 yo"),
    ("EP-6", "name", "Fenmere Ostrik"), ("EP-6", "name", "Quenvoss Embril"), ("EP-6", "dob", "03/14/1950"),
    ("EP-6", "ssn", "906-00-0006"), ("EP-6", "mrn", "ZQ0000106"), ("EP-6", "phone", "202.555.0106"),
    ("EP-6", "address", "16 Drelfen Rise"),
]
# names with no label in front of them: only the model can find these
UNLABELED_NAMES = {"Quenvoss Embril"}
ALL_NAMES = sorted({v for _, k, v in PHI if k == "name"})

EPCRS = [
    ("EP-1", "TR-2026-000001",
     "Wristband reads Drelwick, Talviq. Working dx: STEMI. Born 12 Apr 1961. SSN 901-00-0001.\n"
     "Chart #QV/000101. Recheck: recurrent VT on monitor. Repeat VT made the unmonitored stretch of the road "
     "transfer unsafe. Call back (202) 555-0101. Residence: 11 Quillglass Spur. Physician certification signed. "
     "Loaded leg 88 mi. Talviq was anxious on the pad."),
    ("EP-2", "TR-2026-000002",
     "Report for Embril Q. Vossquill.  Referral impression — head injury. DOB on face sheet: 1958-02-03.\n"
     "On loading, GCS fell from 12 to 8. Highway route discussed at bedside. Sending MD attested to necessity. "
     "Registration ID (SS) 902-00-0002. Sending record MR-0002-X. Family contact 202-555-0102. "
     "Home listed as 12 Vexmere Ct. Loaded leg 64 mi."),
    ("EP-3", "TR-2026-000003",
     "Handoff lists respiratory failure. Per sending RN: SpO2 84% despite mask oxygen. Patient: Orvessa Fernivex. "
     "Available road unit could not maintain the required ventilator settings. DOB 07/30/1944. SSN 903-00-0003. "
     "Chart #ZQ0000103. Call back 202.555.0103. Residence: 13 Drelfen Rise. Physician certification signed. "
     "Loaded leg 180 mi."),
    ("EP-4", "TR-2026-000004",
     "Wristband reads Tarnivell, Nimvian. Working dx: shock. Recheck: MAP 52 on repeat cuff. "
     "Persistent hypotension ruled out the prolonged trip by ambulance. Certification pending at handoff. "
     "Born Mar 9, 1970. SSN 904-00-0004. Chart #QV/000104. Call back (202) 555-0104. "
     "Home listed as 14 Quillglass Spur. Loaded leg 51 mi."),
    ("EP-5", "TR-2026-000005",
     "Report for Zelquinn Quenvoss, 94 yo. Working dx: STEMI. Recheck: recurrent VT on monitor. "
     "ALS unit ETA plus drive time exceeded the accepting cath team's safe interval. Physician certification signed. "
     "DOB 1931-11-11. SSN 905-00-0005. Sending record MR-0005-X. Family contact 202-555-0105. "
     "Residence: 15 Vexmere Ct. Email z.quenvoss@example.org. Member ID QB-55120-7. Acct # 7781-2210. "
     "Loaded leg 40 mi."),
    ("EP-6", "TR-2026-000006",
     "Pt name: Fenmere Ostrik. Working dx: STEMI. Recheck: recurrent VT on monitor. Quenvoss Embril drove in "
     "behind the ambulance and asked about the road. Could not tolerate another hour by road with runs of VT "
     "despite treatment. Physician certification signed. DOB on face sheet: 03/14/1950. Registration ID (SS) "
     "906-00-0006. Sending record ZQ0000106. Family contact 202.555.0106. Home listed as 16 Drelfen Rise. "
     "Loaded leg 70 mi."),
]

TRANSPORTS = [  # transport_id, base_id, tail, date, payer, cpt, miles, icd10, billed, collected, status
    ("TR-2026-000001", "DEN-01", "N412AM", D(2026, 6, 10), "Cairnvale Mutual", "A0431", 88, "I21.3", 13076, 9800, "paid"),
    ("TR-2026-000002", "DEN-01", "N412AM", D(2026, 7, 5), "Cairnvale Mutual", "A0431", 64, "S06.9XAA", 11828, 0, "denied"),
    ("TR-2026-000003", "DEN-01", "N412AM", D(2026, 9, 2), "Cairnvale Mutual", "A0431", 180, "J96.01", 17860, 0, "pending"),
    ("TR-2026-000004", "RIM-02", "N107QX", D(2026, 8, 1), "Quill Basin Plan", "A0430", 51, "R57.9", 11152, 0, "denied"),
    ("TR-2026-000005", "RIM-02", "N107QX", D(2026, 6, 20), "Quill Basin Plan", "A0430", 40, "I21.3", 10580, 8000, "paid"),
    ("TR-2026-000006", "DEN-01", "N412AM", D(2026, 5, 20), "Cairnvale Mutual", "A0431", 70, "I21.3", 12140, 0, "pending"),
    ("TR-2026-000007", "DEN-01", "N412AM", D(2026, 9, 14), "Cairnvale Mutual", "A0431", 60, "I21.3", 11620, 0, "pending"),
]
CLAIMS = [(f"CL-2026-00000{i}", f"TR-2026-00000{i}", t[4], t[3] + dt.timedelta(days=1), t[10],
           "CO-50" if t[10] == "denied" else None, t[9]) for i, t in enumerate(TRANSPORTS, 1)]

DOCS = [  # doc_id, dept, title, status, effective_date, content
    ("POL-CM-1", "BILLING", "Cairnvale Mutual transport policy", "ACTIVE", D(2026, 6, 1),
     "Sec 4.2 Document why the available ground transport was clinically unsafe.\n"
     "Sec 4.3 Include signed physician certification in the chart."),
    ("POL-CM-0", "BILLING", "Cairnvale Mutual transport policy (legacy)", "SUPERSEDED", D(2026, 7, 1),
     "Sec 2.1 Include signed physician certification in the chart."),
    ("POL-CM-2", "BILLING", "Cairnvale Mutual transport policy rev 2", "ACTIVE", D(2026, 8, 15),
     "Sec 4.2 Document why the available ground transport was clinically unsafe.\n"
     "Sec 4.3 Include signed physician certification in the chart.\n"
     "Sec 5.1 Loaded miles may not exceed 150 miles per leg."),
    ("POL-QB-1", "BILLING", "Quill Basin Plan transport policy", "ACTIVE", D(2026, 6, 1),
     "Sec 3.1 State why road ambulance transfer was contraindicated.\n"
     "Sec 3.2 Attach physician certification of medical necessity."),
    ("SOP-1-A", "OPS", "Rotor restart inspection", "SUPERSEDED", D(2026, 4, 1),
     "Sec 4.2 The minimum observation time is 15 minutes."),
    ("SOP-1-B", "OPS", "Rotor restart inspection", "ACTIVE", D(2026, 6, 1),
     "Sec 4.2 The minimum observation time is 30 minutes."),
    ("FIN-REV", "FINANCE", "Base revenue reconciliation", "ACTIVE", D(2026, 6, 1),
     "Sec 1.1 Model the no-outage budget using actual collections plus $7400 in expected net receipts "
     "for each missed AOG request."),
    ("FIN-LAB", "FINANCE", "Recovery expense allocation", "ACTIVE", D(2026, 6, 1),
     "Sec 2.3 Charge recovery overtime at $78 per hour and repair labor at $325 per outage hour."),
    ("HR-DUTY", "HR", "Duty and relief", "ACTIVE", D(2026, 6, 1),
     "Sec 2.1 Duty may not exceed 14 hours per shift. Record all recovery overtime."),
    ("HR-LIC", "HR", "Flight credential checks", "ACTIVE", D(2026, 6, 1),
     "Sec 3.4 Credentials remain valid through the expiry date."),
]

AIRCRAFT = [("N412AM", "DEN-01", "rotary", True), ("N107QX", "RIM-02", "fixed-wing", True)]
MX = [  # mx_id, tail, base, start, end, aog_hours, issue_text, missed_requests
    ("MX-2026-000001", "N412AM", "DEN-01", TS(2026, 9, 14, 6), TS(2026, 9, 14, 20), 14,
     "Hydraulic pressure decayed on run-up; pump removed. Aircraft grounded.", 3),
    ("MX-2026-000002", "N107QX", "RIM-02", TS(2026, 7, 3, 8), TS(2026, 7, 3, 9), 0,
     "Cabin light lens cracked; deferred per MEL.", 0),
]
DISPATCH = [  # request_id, base_id, ts, outcome, transport_id
    ("RQ-1", "DEN-01", TS(2026, 9, 14, 7), "declined_aog", None),
    ("RQ-2", "DEN-01", TS(2026, 9, 14, 9), "declined_aog", None),
    ("RQ-3", "DEN-01", TS(2026, 9, 14, 13), "declined_aog", None),
    ("RQ-4", "DEN-01", TS(2026, 9, 14, 21), "accepted", "TR-2026-000007"),
    ("RQ-5", "RIM-02", TS(2026, 8, 2, 10), "accepted", "TR-2026-000004"),
    ("RQ-6", "RIM-02", TS(2026, 8, 3, 10), "declined_weather", None),
]
LEDGER = [  # base, month, revenue, fuel, maintenance_cost, crew_cost, lease_cost
    ("DEN-01", D(2026, 9, 1), 40000, 20000, 16550, 30312, 18000),
    ("RIM-02", D(2026, 9, 1), 30000, 19000, 12000, 25000, 15000),
]
BUDGET = [  # base, month, line, planned
    ("DEN-01", D(2026, 9, 1), "revenue", 62200), ("DEN-01", D(2026, 9, 1), "fuel", 20000),
    ("DEN-01", D(2026, 9, 1), "maintenance_cost", 12000), ("DEN-01", D(2026, 9, 1), "crew_cost", 30000),
    ("DEN-01", D(2026, 9, 1), "lease_cost", 18000),
    ("RIM-02", D(2026, 9, 1), "revenue", 30000), ("RIM-02", D(2026, 9, 1), "fuel", 19000),
    ("RIM-02", D(2026, 9, 1), "maintenance_cost", 12000), ("RIM-02", D(2026, 9, 1), "crew_cost", 25000),
    ("RIM-02", D(2026, 9, 1), "lease_cost", 15000),
]
CREW = [("CR-2026-000001", "DEN-01", "pilot", D(2020, 1, 1)), ("CR-2026-000002", "DEN-01", "flight nurse", D(2021, 1, 1)),
        ("CR-2026-000003", "RIM-02", "flight paramedic", D(2022, 1, 1)), ("CR-2026-000004", "RIM-02", "mechanic", D(2019, 1, 1))]
LICENSES = [("CR-2026-000001", "flight certificate", D(2026, 9, 1)), ("CR-2026-000002", "RN", D(2027, 3, 1)),
            ("CR-2026-000003", "paramedic", D(2026, 11, 20)), ("CR-2026-000004", "airframe", D(2026, 6, 15))]
SHIFTS = [  # crew_id, date, hours, overtime_hours, reason_text
    ("CR-2026-000001", D(2026, 9, 14), 16, 4, "Recovery cover for N412AM AOG."),
    ("CR-2026-000002", D(2026, 9, 14), 15, 3, "Stayed late; aircraft grounded for hydraulics."),
    ("CR-2026-000002", D(2026, 9, 15), 12, 0, "Scheduled duty."),
    ("CR-2026-000003", D(2026, 8, 2), 13, 1, "Held over, fog at receiving pad."),
    ("CR-2026-000004", D(2026, 8, 2), 12, 0, "Scheduled duty."),
    ("CR-2026-000003", D(2026, 10, 31), 12, 0, "Scheduled duty."),
]

TABLES = {
    "transports": ("transport_id VARCHAR, base_id VARCHAR, tail_number VARCHAR, date DATE, payer VARCHAR, cpt_code VARCHAR, "
                   "loaded_miles INTEGER, icd10_code VARCHAR, billed_amount INTEGER, collected_amount INTEGER, claim_status VARCHAR",
                   TRANSPORTS),
    "epcrs": ("doc_id VARCHAR, transport_id VARCHAR, raw_text VARCHAR", EPCRS),
    "claims": ("claim_id VARCHAR, transport_id VARCHAR, payer VARCHAR, submitted_date DATE, status VARCHAR, "
               "denial_code VARCHAR, paid_amount INTEGER", CLAIMS),
    "documents": ("doc_id VARCHAR, dept VARCHAR, title VARCHAR, status VARCHAR, effective_date DATE, content VARCHAR", DOCS),
    "aircraft": ("tail_number VARCHAR, base_id VARCHAR, type VARCHAR, in_service BOOLEAN", AIRCRAFT),
    "dispatch_log": ("request_id VARCHAR, base_id VARCHAR, ts TIMESTAMP, outcome VARCHAR, transport_id VARCHAR", DISPATCH),
    "maintenance_events": ('mx_id VARCHAR, tail_number VARCHAR, base_id VARCHAR, "start" TIMESTAMP, "end" TIMESTAMP, '
                           "aog_hours INTEGER, issue_text VARCHAR, missed_requests INTEGER", MX),
    "weather_declines": ("request_id VARCHAR, base_id VARCHAR, ts TIMESTAMP, reason_text VARCHAR",
                         [("RQ-6", "RIM-02", TS(2026, 8, 3, 10), "Fog at pad")]),
    "base_ledger": ("base_id VARCHAR, month DATE, revenue INTEGER, fuel INTEGER, maintenance_cost INTEGER, crew_cost INTEGER, "
                    "lease_cost INTEGER", LEDGER),
    "budget": ("base_id VARCHAR, month DATE, line VARCHAR, planned INTEGER", BUDGET),
    "leases": ("tail_number VARCHAR, monthly_cost INTEGER", [("N412AM", 18000), ("N107QX", 15000)]),
    "crew": ("crew_id VARCHAR, base_id VARCHAR, role VARCHAR, hire_date DATE", CREW),
    "licenses": ("crew_id VARCHAR, kind VARCHAR, expires DATE", LICENSES),
    "shifts": ("crew_id VARCHAR, date DATE, hours INTEGER, overtime_hours INTEGER, reason_text VARCHAR", SHIFTS),
}


def build(con=None):
    con = con or duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA IF NOT EXISTS bronze")
    for name, (cols, rows) in TABLES.items():
        con.execute(f"CREATE OR REPLACE TABLE bronze.{name} ({cols})")
        if rows:
            n = len(rows[0])
            con.executemany(f"INSERT INTO bronze.{name} VALUES ({', '.join(['?'] * n)})", rows)
    return con


def phi_values():
    """Every planted PHI string plus each word of each name (>= 3 letters)."""
    vals = {v for _, _, v in PHI}
    for n in ALL_NAMES:
        vals |= {w.strip(",.") for w in n.split() if len(w.strip(",.")) >= 3}
    return sorted(vals)


class FakeNER:
    """Fake `local` provider for wave1.billing.redact_names: a perfect NER
    that returns every fixture name present in the chart in the prompt."""
    def __init__(self):
        self.calls = 0

    def __call__(self, prompt, model, json_mode=False, **_):
        import json
        self.calls += 1
        chart = re.search(r"<<<\n(.*)\n>>>", prompt, re.S).group(1)
        return json.dumps({"names": [n for n in ALL_NAMES if n in chart] + ["Not In Chart"]}), {}
