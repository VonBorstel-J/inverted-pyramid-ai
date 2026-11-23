"""Waves on the in-test bronze fixture. Fake model providers only."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import bronze_fixture as B  # noqa: E402

from llm import log  # noqa: E402
from llm import providers as P  # noqa: E402
from waves import billing, pipeline  # noqa: E402
from waves.framework import PROV_COLS, Ctx, StepResult, gate  # noqa: E402
from waves.schemas import EpcrExtractRow  # noqa: E402

SILVER = ["epcr_deid", "epcr_extract", "claim_reconciliation", "mx_classified", "doc_sections", "aog_impact",
          "ledger_lines", "finance_variance", "license_status", "overtime_causes", "hr_compliance_flags", "aog_overtime"]


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.logp = os.path.join(self.td.name, "routing.jsonl")
        p = mock.patch.dict(os.environ, {"IPA_ROUTING_LOG": self.logp})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.td.cleanup)
        self.con = B.build()
        self.addCleanup(self.con.close)

    def fake(self, name, fn):
        p = mock.patch.dict(P.PROVIDERS, {name: fn})
        p.start()
        self.addCleanup(p.stop)

    def deid(self):
        return dict(self.con.execute("SELECT doc_id, deid_text FROM silver.epcr_deid").fetchall())


class TestRedaction(Base):
    def test_recall_100_percent_with_model_names(self):
        ner = B.FakeNER()
        self.fake("local", ner)
        r = billing.redact_step(self.con, Ctx(providers={"wave1.billing.redact_names": "local"}))
        self.assertEqual((r.rows_out, r.rows_rejected, r.provider), (6, 0, "local"))
        self.assertEqual(ner.calls, 6)
        deid = self.deid()
        hits, total = {}, {}
        for doc, kind, value in B.PHI:
            total[kind] = total.get(kind, 0) + 1
            leaked = value in deid[doc] or (kind == "name" and any(
                w.strip(",.") in deid[doc] for w in value.split() if len(w.strip(",.")) >= 3))
            hits[kind] = hits.get(kind, 0) + (0 if leaked else 1)
        recall = {k: hits[k] / total[k] for k in total}
        self.assertEqual(recall, {k: 1.0 for k in total}, recall)
        self.assertNotIn("Not In Chart", json.dumps(deid))  # hallucinated name ignored harmlessly
        rows = log.read(self.logp)
        self.assertEqual({(x["task"], x["provider"]) for x in rows}, {("wave1.billing.redact_names", "local")})

    def test_rules_alone_miss_only_the_unlabeled_name(self):
        billing.redact_step(self.con, Ctx())
        deid = self.deid()
        for doc, kind, value in B.PHI:
            if value in B.UNLABELED_NAMES:
                self.assertIn(value, deid[doc])  # this is what the model is for
            else:
                self.assertNotIn(value, deid[doc], (doc, kind))

    def test_names_task_never_sent_to_cloud(self):
        called = []
        self.fake("claude", lambda *a, **k: called.append(1) or ('{"names": []}', {}))
        r = billing.redact_step(self.con, Ctx(providers={"wave1.billing": "claude"}))
        self.assertEqual(called, [])
        self.assertEqual(r.provider, "rules")
        self.assertTrue(any("may not see raw PHI" in n for n in r.notes))

    def test_cloud_extraction_sees_only_deid_text(self):
        self.fake("local", B.FakeNER())
        prompts = []

        def claude(prompt, model, **_):
            prompts.append(prompt)
            return json.dumps({"diagnosis": "x", "physician_certification": True, "loaded_miles": 1}), {}
        self.fake("claude", claude)
        ctx = Ctx(providers={"wave1.billing.redact_names": "local", "wave1.billing.extract": "claude"})
        billing.redact_step(self.con, ctx)
        r = billing.extract_step(self.con, ctx)
        self.assertEqual((r.rows_out, len(prompts)), (6, 6))
        for p in prompts:
            for v in B.phi_values():
                self.assertNotIn(v, p)

    def test_model_error_falls_back_to_rules(self):
        self.fake("local", lambda *a, **k: (_ for _ in ()).throw(P.ProviderError("down")))
        r = billing.redact_step(self.con, Ctx(providers={"wave1.billing.redact_names": "local"}))
        self.assertEqual(r.rows_out, 6)
        self.assertTrue(any("fell back" in n for n in r.notes))
        self.assertNotIn("901-00-0001", self.deid()["EP-1"])


class TestExtraction(Base):
    def test_rules_baseline_fields(self):
        billing.redact_step(self.con, Ctx())
        billing.extract_step(self.con, Ctx())
        rows = {r[0]: r[1:] for r in self.con.execute(
            "SELECT doc_id, diagnosis, ground_contraindication IS NOT NULL, physician_certification, loaded_miles "
            "FROM silver.epcr_extract").fetchall()}
        self.assertEqual(rows["EP-1"], ("STEMI", True, True, 88))
        self.assertEqual(rows["EP-2"], ("head injury", False, True, 64))
        self.assertEqual(rows["EP-4"], ("shock", True, False, 51))
        self.assertEqual(rows["EP-3"][3], 180)

    def test_rejects_quarantine(self):
        billing.redact_step(self.con, Ctx())
        deid = self.deid()

        def claude(prompt, model, **_):
            if "Repeat VT" in prompt:          # EP-1: never valid JSON -> rejected after one retry
                return "I cannot help with that", {}
            if "GCS fell" in prompt:           # EP-2: paraphrased span -> rejected by the verbatim gate
                return json.dumps({"ground_contraindication": "ground was unsafe",
                                   "physician_certification": True}), {}
            return json.dumps({"physician_certification": False}), {}
        self.fake("claude", claude)
        r = billing.extract_step(self.con, Ctx(providers={"wave1": "claude"}))
        self.assertEqual((r.rows_out, r.rows_rejected), (4, 2))
        rej = dict(self.con.execute("SELECT prov_source, reason FROM silver.epcr_extract_rejects").fetchall())
        self.assertIn("RouterError", rej["EP-1"])
        self.assertIn("verbatim", rej["EP-2"])
        self.assertTrue(deid)

    def test_json_retry_in_a_wave(self):
        billing.redact_step(self.con, Ctx())
        seen = {}

        def local(prompt, model, **_):
            k = prompt.split(">>>")[0][-60:]
            seen[k] = seen.get(k, 0) + 1
            if seen[k] == 1:
                return '{"loaded_miles": "many"}', {}
            return '{"physician_certification": true}', {}
        self.fake("local", local)
        r = billing.extract_step(self.con, Ctx(providers={"wave1.billing.extract": "local"}))
        self.assertEqual((r.rows_out, r.rows_rejected), (6, 0))
        oks = [x["ok"] for x in log.read(self.logp) if x["task"] == "wave1.billing.extract"]
        self.assertEqual(oks.count(False), 6)
        self.assertEqual(oks.count(True), 6)

    def test_gate_validates_directly(self):
        r = StepResult("t", 1, "silver.t")
        prov = dict.fromkeys(PROV_COLS)
        gate(self.con, "silver.t", EpcrExtractRow,
             [({"doc_id": "a", "transport_id": "b", "loaded_miles": -5}, {**prov, "prov_source": "a", "prov_step": "t"}),
              ({"doc_id": "c", "transport_id": "d", "loaded_miles": 5}, {**prov, "prov_source": "c", "prov_step": "t"})],
             r, None)
        self.assertEqual((r.rows_out, r.rows_rejected), (1, 1))
        reason = self.con.execute("SELECT reason FROM silver.t_rejects").fetchone()[0]
        self.assertIn("loaded_miles", reason)


class TestReconciliation(Base):
    def setUp(self):
        super().setUp()
        pipeline.run(self.con, Ctx(), gold=False, governance=False)
        cur = self.con.execute("SELECT * FROM silver.claim_reconciliation")
        cols = [d[0] for d in cur.description]
        self.rec = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}

    def test_policy_in_effect_on_date_never_superseded(self):
        # 2026-07-05: POL-CM-0 (SUPERSEDED, effective 07-01) is newer but must not be used
        r = self.rec["CL-2026-000002"]
        self.assertEqual((r["policy_doc_id"], r["at_risk"], r["reason"]), ("POL-CM-1", True, "missing ground contraindication"))
        self.assertEqual(json.loads(r["citation"]), ["POL-CM-1", "Sec 4.2", "2026-06-01"])
        # 2026-09-02: rev 2 (effective 08-15) is in effect and caps miles
        r = self.rec["CL-2026-000003"]
        self.assertEqual((r["policy_doc_id"], r["policy_section"], r["reason"]), ("POL-CM-2", "Sec 5.1", "loaded miles over policy cap"))
        self.assertEqual(self.rec["CL-2026-000001"]["policy_doc_id"], "POL-CM-1")
        self.assertFalse(self.rec["CL-2026-000001"]["at_risk"])
        self.assertEqual(self.rec["CL-2026-000004"]["reason"], "missing certification")
        self.assertIsNone(self.rec["CL-2026-000006"]["policy_doc_id"])  # before any policy
        used = {r["policy_doc_id"] for r in self.rec.values()}
        self.assertNotIn("POL-CM-0", used)

    def test_policy_in_effect_unit(self):
        import datetime as dt
        pols = [{"doc_id": "A", "title": "P plan", "status": "SUPERSEDED", "effective_date": dt.date(2026, 1, 1)}]
        self.assertIsNone(billing.policy_in_effect(pols, "P plan", dt.date(2026, 6, 1)))
        pols.append({"doc_id": "B", "title": "P plan", "status": "ACTIVE", "effective_date": dt.date(2026, 7, 1)})
        self.assertIsNone(billing.policy_in_effect(pols, "P plan", dt.date(2026, 6, 1)))
        self.assertEqual(billing.policy_in_effect(pols, "P plan", dt.date(2026, 7, 1))["doc_id"], "B")

    def test_sections_json_supported(self):
        doc = {"sections": json.dumps({"Sec 4.2": "ground unsafe", "Sec 4.3": "physician certification"}), "content": ""}
        self.assertEqual([r[:2] for r in billing.policy_requirements(doc)],
                         [("ground_contraindication", "Sec 4.2"), ("physician_certification", "Sec 4.3")])


class TestOtherDepartments(Base):
    def setUp(self):
        super().setUp()
        pipeline.run(self.con, Ctx(), gold=True, governance=False)

    def q(self, sql):
        return self.con.execute(sql).fetchall()

    def test_ops(self):
        self.assertEqual(self.q("SELECT system, issue_category, severity FROM silver.mx_classified ORDER BY mx_id"),
                         [("hydraulic", "pressure loss", "AOG"), ("other", "crack", "deferred")])
        self.assertEqual(self.q("SELECT est_lost_transports, est_lost_revenue FROM silver.aog_impact"), [(3, 22200.0)])
        self.assertEqual(self.q("SELECT count(*) FROM silver.doc_sections WHERE status <> 'ACTIVE' OR doc_id = 'SOP-1-A'"), [(0,)])

    def test_finance(self):
        rows = dict((r[0], r[1:]) for r in self.q(
            "SELECT line, variance, driver FROM silver.finance_variance WHERE base_id = 'DEN-01'"))
        self.assertEqual(rows["revenue"], (-22200.0, "AOG missed requests"))
        self.assertEqual(rows["crew_cost"], (312.0, "AOG recovery overtime"))
        self.assertNotIn("fuel", rows)

    def test_hr(self):
        flags = self.q("SELECT crew_id, flag FROM silver.hr_compliance_flags ORDER BY 1, 2")
        self.assertIn(("CR-2026-000001", "expired_license_on_flown_shift"), flags)
        self.assertIn(("CR-2026-000001", "duty_limit_breach"), flags)
        self.assertNotIn(("CR-2026-000004", "expired_license_on_flown_shift"), flags)  # mechanic never flies
        self.assertEqual(self.q("SELECT sum(overtime_cost) FROM silver.aog_overtime"), [(546.0,)])
        self.assertEqual(self.q("SELECT cause FROM silver.overtime_causes WHERE crew_id='CR-2026-000003'"), [("weather",)])

    def test_gold_cross_department(self):
        row = self.q("SELECT est_lost_revenue, overtime_cost, est_total_cost FROM gold.aog_events")
        self.assertEqual(row, [(22200.0, 546.0, 22746.0)])
        self.assertGreater(self.q("SELECT count(*) FROM gold.entity_links")[0][0], 20)

    def test_provenance_on_every_silver_table(self):
        for t in SILVER:
            cols = [r[0] for r in self.q(f"SELECT column_name FROM information_schema.columns "
                                         f"WHERE table_schema='silver' AND table_name='{t}'")]
            for c in PROV_COLS:
                self.assertIn(c, cols, (t, c))
            self.assertEqual(self.q(f"SELECT count(*) FROM silver.{t} WHERE prov_step IS NULL OR prov_at IS NULL"), [(0,)])

    def test_idempotent(self):
        def snap():
            return {t: sorted(map(str, self.q(f"SELECT * EXCLUDE (prov_at) FROM silver.{t}"))) for t in SILVER}
        first = snap()
        pipeline.run(self.con, Ctx(run_at=Ctx().run_at), gold=True, governance=False)
        self.assertEqual(first, snap())


class TestPipelineCli(unittest.TestCase):
    def test_provider_prefixes(self):
        ctx = Ctx(providers=pipeline.parse_providers(["wave1=local", "wave1.billing.extract=claude"]))
        self.assertEqual(ctx.provider("wave1.billing.extract"), "claude")
        self.assertEqual(ctx.provider("wave1.ops.classify"), "local")
        self.assertEqual(ctx.provider("wave2.x"), "rules")
        with self.assertRaises(SystemExit):
            pipeline.parse_providers(["wave1=gpt"])


if __name__ == "__main__":
    unittest.main()
