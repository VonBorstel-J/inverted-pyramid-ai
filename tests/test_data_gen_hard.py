import hashlib
import json
import re
import tempfile
import unittest
from collections import Counter
from datetime import date
from pathlib import Path

import duckdb

from data_gen import hard_templates
from data_gen.common import SCHEMAS
from data_gen.generate_all import generate
from tests import test_data_gen as baseline

DATA = Path(hard_templates.__file__).parent


def checksum(db, table):
    rows = db.execute(f'SELECT * FROM bronze.{table} ORDER BY ALL').fetchall()
    return hashlib.sha256(json.dumps(rows, default=str, ensure_ascii=False).encode()).hexdigest()


class HardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(dir=DATA)
        cls.addClassCleanup(tmp.cleanup)
        cls.root = Path(tmp.name)
        path = cls.root / 'hard.duckdb'
        cls.counts = generate(42, path, 'hard')
        cls.db = duckdb.connect(str(path))
        cls.addClassCleanup(cls.db.close)
        cls.db.execute("SET schema='bronze'")

    def rows(self, sql):
        return self.db.execute(sql).fetchall()

    def test_default_checksums_and_hard_determinism(self):
        other = self.root / 'other.duckdb'
        generate(42, other, 'hard')
        with duckdb.connect(str(other)) as db:
            tables = dict(db.execute("SELECT table_name,table_type FROM information_schema.tables WHERE table_schema='bronze'").fetchall())
            self.assertEqual(set(tables), set(SCHEMAS) | {'truth_policy_version'})
            for name in tables:
                self.assertEqual(checksum(db, name), checksum(self.db, name), name)
        generate(42, other)
        expected = json.loads((DATA / 'default_seed42_checksums.json').read_text())
        with duckdb.connect(str(other)) as db:
            tables = db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='bronze'").fetchall()
            self.assertEqual({name: checksum(db, name) for name, in tables}, expected)

    test_key_integrity = baseline.BronzeTests.test_key_integrity
    test_variances_and_shift_flags = baseline.BronzeTests.test_variances_and_shift_flags

    def test_identifier_inventory_and_clinical_offsets(self):
        patterns = {
            'name': r'\b(?:Marisol|Wren|Briar|Reed|Vale|Sable|Pip|Sunny|Tansy|Ilex Venn|Neri Brume)\b',
            'dob': r'the third of May, ’\d\d', 'age': r'now (ninety-three)',
            'ssn': r'(?:SS suffix |SS card ends )(\d{4})',
            'address': r'\d+ Larkglass Mews', 'mrn': r'KX-\d\d\n\d\d-R',
            'phone': r'202 555 0142 ext \d+|two zero two five five five zero one four two',
            'email': r'cot\d+@example\.invalid', 'plate': r'QZ-\d{4}-V',
            'serial': r'VX/\d{5}/Q', 'url': r'https://example\.invalid/cot/\d+',
        }
        expected = Counter()
        labels = self.rows("""SELECT doc_id,raw_text,diagnosis,instability_evidence,has_contraindication,
            contraindication,span_start,span_end FROM epcrs JOIN truth_epcrs USING(doc_id,transport_id)""")
        self.assertEqual(len(labels), 2000)
        self.assertEqual(sum(not row[4] for row in labels), 500)
        for doc, raw, diagnosis, evidence, present, phrase, lo, hi in labels:
            self.assertIn(diagnosis, raw)
            self.assertIn(evidence, raw)
            for kind, pattern in patterns.items():
                matches = list(re.finditer(pattern, raw))
                required = 4 if kind == 'name' else (int('’33' in raw) if kind == 'age' else 1)
                self.assertEqual(len(matches), required, (doc, kind))
                for match in matches:
                    group = 1 if kind in ('age', 'ssn') else 0
                    expected[doc, kind, match[group], *match.span(group)] += 1
            if present:
                self.assertEqual(raw[lo:hi], phrase)
                self.assertIn(phrase, hard_templates.ROAD)
            else:
                self.assertEqual((phrase, lo, hi), (None, None, None))
                self.assertTrue(any(phrase in raw for phrase in hard_templates.MISSING))
        self.assertEqual(Counter(self.rows('SELECT * FROM truth_phi')), expected)

    def test_certification_and_miles(self):
        seen = set()
        for raw, signed, miles, billed in self.rows("""SELECT raw_text,physician_certification,
                e.loaded_miles,t.loaded_miles FROM truth_epcrs e JOIN epcrs USING(doc_id,transport_id)
                JOIN transports t USING(transport_id)"""):
            form = [state for phrase, state in hard_templates.CERT if phrase in raw]
            self.assertEqual(form, [signed])
            self.assertEqual(miles, billed)
            if 'statute miles.' in raw and 'we logged' in raw:
                seen.add('words')
                self.assertIn(f'logged {hard_templates.words(miles)} statute miles.', raw)
            elif 'midpoint' in raw:
                seen.add('range')
                lo, hi = map(int, re.search(r'(\d+)–(\d+) mi', raw).groups())
                self.assertLess(lo, hi)
                self.assertEqual(lo + hi, 2*miles)
            elif ' NM' in raw:
                seen.add('nautical')
                nautical = float(re.search(r'([\d.]+) NM', raw)[1])
                self.assertEqual(round(nautical * 1852 / 1609.344), miles)
            else:
                seen.add('corrected')
                self.assertIn(f'Postflight amendment: {miles} mi with patient.', raw)
        self.assertEqual(seen, {'words', 'range', 'nautical', 'corrected'})

    def test_policy_history_and_counterfactual(self):
        joined = self.rows("""SELECT c.claim_id,c.payer,t.date,c.submitted_date,v.transport_date,v.version,
            v.doc_id,r.doc_id,d.status,v.effective_date,d.effective_date,e.has_contraindication,
            e.physician_certification,t.loaded_miles,r.at_risk,r.reason,r.section,d.sections
            FROM claims c JOIN transports t USING(transport_id,payer)
            JOIN truth_epcrs e USING(transport_id) JOIN truth_policy_version v USING(claim_id,payer)
            JOIN truth_claim_risk r USING(claim_id) JOIN documents d ON d.doc_id=v.doc_id""")
        cut = date(2026, 8, 15)
        self.assertEqual(len(joined), 2000)
        self.assertEqual(len({row[0] for row in joined}), 2000)
        after = changed = crossing = 0
        boundary = set()
        for (_, payer, day, submitted, service, version, doc, citation, status, effective, doc_date,
             ground, signed, miles, risk, reason, sec, sections) in joined:
            index = ('Cairnvale Mutual', 'Fenmere Benefit Trust', 'Quill Basin Plan').index(payer)
            revised = index < 2 and day >= cut
            boundary.add((index, day))
            crossing += index < 2 and day < cut <= submitted
            self.assertEqual((service, version, effective, doc_date),
                             (day, 1+revised, cut if revised else date(2026, 6, 1), effective))
            self.assertEqual((doc, citation), (f'POL-{index}-V{version}', doc))
            self.assertEqual(status, 'SUPERSEDED' if index < 2 and not revised else 'ACTIVE')
            if not ground:
                label = ('missing ground contraindication', 'Sec 4.2')
            elif not signed and (index == 2 or (revised and index == 0)):
                label = ('missing certification', 'Sec 4.3')
            elif miles > 100 and revised and index == 1:
                label = ('exceeds mileage cap', 'Sec 4.4')
            else:
                label = ('meets policy', 'Sec 4.2')
            self.assertEqual((reason, sec, risk), (*label, label[0] != 'meets policy'))
            self.assertIn(sec, json.loads(sections))
            if day >= cut:
                after += 1
                old_risk = not ground or (index == 2 and not signed)
                changed += risk != old_risk
        self.assertTrue(all((index, cut) in boundary for index in (0, 1)))
        self.assertGreater(crossing, 0)
        self.assertGreaterEqual(changed / after, .15)

    def test_search_corpus_and_tail_aliases(self):
        baseline.BronzeTests.test_expiries_and_policies(self)
        self.assertEqual(self.rows('SELECT count(*) FROM truth_sop_traps'), [(3,)])
        for dept in ('BILLING', 'OPS', 'HR', 'FINANCE'):
            docs = self.db.execute('SELECT sections FROM documents WHERE dept=?', [dept]).fetchall()
            self.assertGreaterEqual(len(docs), 12)
            self.assertGreaterEqual(sum(len(json.loads(s)) for s, in docs), 40)
        rows = self.rows("""SELECT tail_number,issue_text,reason_text FROM maintenance_events
            JOIN shifts USING(mx_id) JOIN truth_overtime USING(mx_id,shift_id)""")
        self.assertEqual(len(rows), self.counts['truth_overtime'])
        for tail, issue, reason in rows:
            self.assertIn(tail[1:4] + ' bird', issue)
            self.assertIn('H135 at DEN' if tail == 'N412AM' else tail[1:4] + ' bird', reason)

    def test_phrasing_is_held_out(self):
        phrases = ('Working dx:', 'Referral impression', 'Handoff lists', 'Per sending RN:',
                   'Recheck:', 'On loading,', 'Loaded leg', 'Physician certification signed.',
                   'Sending MD attested', 'Certification pending at handoff', 'Wristband reads',
                   'DOB on face sheet', 'Registration ID (SS)', 'Recovery cover for', 'Scheduled duty.',
                   'Ground transfer field left blank', 'Highway route discussed at bedside')
        texts = [Path(hard_templates.__file__).read_text()]
        texts.extend(raw for raw, in self.rows('SELECT raw_text FROM epcrs'))
        corpus = '\n'.join(texts).casefold()
        for phrase in phrases:
            self.assertNotIn(phrase.casefold(), corpus)
