import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

import duckdb

from data_gen.common import BASES, END, SCHEMAS, START
from data_gen.generate_all import generate


class BronzeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "data_gen")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.path = Path(cls.temp.name) / "one.duckdb"
        generate(42, cls.path)
        cls.db = duckdb.connect(str(cls.path))
        cls.addClassCleanup(cls.db.close)
        cls.db.execute("SET schema='bronze'")

    def rows(self, sql):
        return self.db.execute(sql).fetchall()

    def test_determinism(self):
        other = Path(self.temp.name) / "two.duckdb"
        generate(42, other)

        def digest(db, name):
            rows = db.execute(f"SELECT * FROM bronze.{name} ORDER BY ALL").fetchall()
            return hashlib.sha256(json.dumps(rows, default=str, ensure_ascii=False).encode()).digest()

        with duckdb.connect(str(other)) as second:
            self.assertEqual(set(SCHEMAS), {r[0] for r in self.rows("SHOW TABLES")})
            for name in SCHEMAS:
                with self.subTest(table=name):
                    self.assertEqual(digest(self.db, name), digest(second, name))

    def test_key_integrity(self):
        parents = dict(tail_number="aircraft", crew_id="crew", transport_id="transports",
                       claim_id="claims", request_id="dispatch_log", mx_id="maintenance_events",
                       shift_id="shifts")
        for col, parent in parents.items():
            self.assertEqual(self.rows(f"SELECT count(*)-count(distinct {col}) FROM {parent}"), [(0,)])
        self.assertEqual(set(BASES), {x[0] for x in self.rows("SELECT DISTINCT base_id FROM aircraft")})
        for table, cols in SCHEMAS.items():
            for col in cols:
                if col == "base_id":
                    source = "SELECT base_id FROM aircraft"
                elif col in ("doc_id", "active_doc_id", "superseded_doc_id"):
                    target = "epcrs" if table in ("epcrs", "truth_phi", "truth_epcrs") else "documents"
                    source = f"SELECT doc_id FROM {target}"
                elif col in parents:
                    source = f"SELECT {col} FROM {parents[col]}"
                else:
                    continue
                with self.subTest(table=table, key=col):
                    self.assertFalse(self.rows(f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL EXCEPT {source}"))
        for child, parent, cols in (
            ("maintenance_events", "aircraft", "base_id,tail_number"),
            ("dispatch_log", "aircraft", "base_id,tail_number"),
            ("truth_license_expiries", "licenses", "crew_id,kind,expires"),
            ("budget", "base_ledger", "base_id,month"),
            ("truth_ledger_variances", "budget", "base_id,month,line,planned"),
            ("truth_overtime", "shifts", "shift_id,mx_id,overtime_hours"),
        ):
            self.assertFalse(self.rows(f"SELECT {cols} FROM {child} EXCEPT SELECT {cols} FROM {parent}"))
        self.assertFalse(self.rows("""SELECT transport_id,base_id,tail_number,date FROM transports EXCEPT
            SELECT transport_id,base_id,tail_number,ts::date FROM dispatch_log WHERE outcome='accepted'"""))
        self.assertFalse(self.rows("SELECT transport_id,payer FROM claims EXCEPT SELECT transport_id,payer FROM transports"))
        for table in ("claims", "epcrs", "transports"):
            self.assertEqual(self.rows(f"SELECT count(*),count(distinct transport_id) FROM {table}"), [(2000, 2000)])

    def test_clinical_and_phi_truth(self):
        notes = dict(self.rows("SELECT doc_id,raw_text FROM epcrs"))
        patterns = {
            "name": r"(?:Talviq|Embril|Orvessa|Nimvian|Zelquinn)(?: Q\.)? (?:Drelwick|Vossquill|Fernivex|Tarnivell|Quenvoss)|(?:Drelwick|Vossquill|Fernivex|Tarnivell|Quenvoss), (?:Talviq|Embril|Orvessa|Nimvian|Zelquinn)",
            "dob": r"\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}|\d{2} [A-Z][a-z]{2} \d{4}",
            "ssn": r"9\d\d-00-\d{4}", "mrn": r"QV/\d{6}|MR-\d{4}-X|ZQ\d{7}",
            "phone": r"\(202\) 555-01\d\d|202[.-]555[.-]01\d\d",
            "address": r"\d+ (?:Quillglass Spur|Vexmere Ct|Drelfen Rise)",
        }
        expected = set()
        for doc, raw in notes.items():
            for kind, pattern in patterns.items():
                matches = list(re.finditer(pattern, raw))
                self.assertEqual(len(matches), 1)
                m = matches[0]
                expected.add((doc, kind, m.group(), *m.span()))
        recorded = self.rows("SELECT * FROM truth_phi")
        self.assertEqual(len(recorded), len(expected))
        self.assertEqual(set(recorded), expected)
        labels = self.rows("SELECT doc_id, has_contraindication, contraindication, span_start, span_end FROM truth_epcrs")
        self.assertEqual({r[0] for r in labels}, set(notes))
        self.assertEqual(sum(not r[1] for r in labels), 500)
        self.assertGreaterEqual(len({r[2] for r in labels if r[1]}), 8)
        for doc, present, phrase, lo, hi in labels:
            if present:
                self.assertEqual(notes[doc][lo:hi], phrase)
            else:
                self.assertEqual((phrase, lo, hi), (None, None, None))

    def test_aog_accounting(self):
        events = self.rows("SELECT mx_id,missed_requests FROM maintenance_events")
        self.assertIn(len(events), range(5, 9))
        for mx, missed in events:
            declined = self.db.execute("""SELECT d.request_id FROM dispatch_log d JOIN maintenance_events m
                USING(mx_id,tail_number,base_id) WHERE mx_id=? AND d.ts >= m.start AND d.ts < m.end
                AND d.outcome='declined_aog' AND transport_id IS NULL""", [mx]).fetchall()
            impacts = self.db.execute("SELECT request_id FROM truth_aog_impacts WHERE mx_id=? AND lost_transports=1", [mx]).fetchall()
            self.assertEqual(set(declined), set(impacts))
            self.assertEqual(len(declined), missed)
            self.assertGreater(missed, 0)
            self.assertGreater(self.db.execute("""SELECT count(*) FROM shifts s JOIN crew USING(crew_id)
                JOIN maintenance_events m USING(mx_id,base_id) WHERE mx_id=? AND date=m.start::date
                AND overtime_hours>0 AND contains(reason_text,m.tail_number)""", [mx]).fetchone()[0], 0)
        self.assertFalse(self.rows("""SELECT 1 FROM dispatch_log d JOIN maintenance_events m USING(tail_number)
            WHERE outcome='accepted' AND ts>=m.start AND ts<m.end"""))
        self.assertFalse(self.rows("""SELECT 1 FROM budget JOIN base_ledger USING(base_id,month)
            LEFT JOIN (SELECT base_id,month,sum(lost_revenue) loss FROM truth_aog_impacts GROUP BY ALL)
            USING(base_id,month) WHERE line='revenue' AND planned-revenue<>coalesce(loss,0)"""))
        self.assertFalse(self.rows("""SELECT base_id,month,revenue FROM base_ledger EXCEPT SELECT base_id,
            date_trunc('month',date)::date,sum(collected_amount) FROM transports GROUP BY ALL"""))

    def test_variances_and_shift_flags(self):
        actual = self.rows("""WITH lines AS (UNPIVOT base_ledger ON revenue,fuel,maintenance_cost,
            crew_cost,lease_cost INTO NAME line VALUE actual)
            SELECT base_id,month,line,planned,actual,actual-planned FROM lines
            JOIN budget USING(base_id,month,line) WHERE actual<>planned""")
        self.assertEqual(set(actual), set(self.rows("SELECT * EXCLUDE(driver) FROM truth_ledger_variances")))
        flags = self.rows("""SELECT shift_id,hours>14,flown AND date>expires FROM shifts JOIN licenses
            USING(crew_id) WHERE hours>14 OR (flown AND date>expires)""")
        self.assertEqual(set(flags), set(self.rows("SELECT * FROM truth_shift_flags")))
        self.assertFalse(self.rows("""SELECT shift_id,mx_id,overtime_hours FROM shifts WHERE overtime_hours>0
            EXCEPT SELECT shift_id,mx_id,overtime_hours FROM truth_overtime"""))

    def test_expiries_and_policies(self):
        expected = self.db.execute("SELECT * FROM licenses WHERE expires BETWEEN ? AND ?", [START, END]).fetchall()
        self.assertTrue(expected)
        self.assertEqual(set(expected), set(self.rows("SELECT * FROM truth_license_expiries")))
        traps = self.rows("""SELECT a.status,b.status,a.title,b.title,a.sections,b.sections,section,active_answer,
            superseded_answer FROM truth_sop_traps t JOIN documents a ON a.doc_id=t.active_doc_id
            JOIN documents b ON b.doc_id=t.superseded_doc_id""")
        self.assertGreaterEqual(len(traps), 2)
        for active, old, title, previous, a, b, sec, new, retired in traps:
            self.assertEqual((active, old, title), ("ACTIVE", "SUPERSEDED", previous))
            self.assertNotEqual(new, retired)
            self.assertIn(new, json.loads(a)[sec])
            self.assertIn(retired, json.loads(b)[sec])

    def test_synthetic_names(self):
        deny = re.compile(r"Air Methods|Air Evac Lifeteam|John Doe|Jane Doe|John Smith", re.I)
        for table, cols in SCHEMAS.items():
            fields = [c for c, typ in cols.items() if typ in ("VARCHAR", "JSON")]
            for row in self.rows(f"SELECT {','.join(fields)} FROM {table}"):
                for col, value in zip(fields, row):
                    if value:
                        self.assertIsNone(deny.search(value))
                        if col != "payer":
                            self.assertNotIn("medicare", value.lower())
