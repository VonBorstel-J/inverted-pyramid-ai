"""evals/: metric math on tiny known inputs, routing-log quality write-back,
and one end-to-end run on the bronze fixture with fake providers and fake
embeddings (no network, no model)."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import bronze_fixture as B  # noqa: E402
from test_answer import FakeWriter  # noqa: E402
from test_search import FakeEmbed  # noqa: E402

from evals import metrics as M  # noqa: E402
from evals import run as R  # noqa: E402
from evals import score  # noqa: E402
from llm import log  # noqa: E402
from llm import providers as P  # noqa: E402
from answer import check as C  # noqa: E402


class TestMetricMath(unittest.TestCase):
    def test_field_accuracy(self):
        truth = {"diagnosis": "STEMI", "instability_evidence": "recurrent VT on monitor", "physician_certification": True,
                 "loaded_miles": 88, "has_contraindication": True,
                 "contraindication": "Repeat VT made the unmonitored stretch of the road transfer unsafe."}
        good = {"diagnosis": "stemi.", "instability_evidence": "Recheck: recurrent VT on monitor",
                "physician_certification": True, "loaded_miles": 88,
                "ground_contraindication": "Repeat VT made the unmonitored stretch of the road transfer unsafe."}
        bad = {"diagnosis": "chest pain", "instability_evidence": None, "physician_certification": False,
               "loaded_miles": 80, "ground_contraindication": None}
        s1, s2 = M.score_extract(good, truth), M.score_extract(bad, truth)
        self.assertTrue(all(s1.values()))
        self.assertFalse(any(s2.values()))
        acc = M.field_accuracy([s1, s2, M.score_extract(None, truth), s1])
        self.assertEqual(acc["diagnosis"], 0.5)
        self.assertEqual(acc["overall"], 0.5)
        # no contraindication in truth: only a null prediction is right
        t2 = dict(truth, has_contraindication=False, contraindication=None)
        self.assertTrue(M.score_extract(bad, t2)["ground_contraindication"])
        self.assertFalse(M.score_extract(good, t2)["ground_contraindication"])

    def test_text_match_bounds(self):
        self.assertTrue(M.text_match("Working dx: STEMI", "STEMI"))
        self.assertFalse(M.text_match("STEMI with a very long trailing explanation of many words", "STEMI"))
        self.assertTrue(M.span_match("Repeat VT made the unmonitored stretch", "Repeat VT made the unmonitored stretch of road"))
        self.assertFalse(M.span_match("Repeat VT", "Repeat VT made the unmonitored stretch of the road transfer unsafe"))

    def test_redaction_recall_precision(self):
        raw = "Pt Ann Vex, DOB 01/02/1950, phone 202-555-0101, seen 2026-06-01."
        deid = "Pt [NAME], DOB [DATE], phone [PHONE], seen [DATE]."
        truth = [{"phi_type": "name", "value": "Ann Vex", "span_start": 3, "span_end": 10},
                 {"phi_type": "dob", "value": "01/02/1950", "span_start": 16, "span_end": 26},
                 {"phi_type": "phone", "value": "202-555-0101", "span_start": 34, "span_end": 46}]
        self.assertEqual(M.redacted_ranges(raw, deid), [(3, 10), (16, 26), (34, 46), (53, 63)])
        r = M.score_redaction(raw, deid, truth)
        self.assertEqual(r["by_type"], {"name": [1, 1], "dob": [1, 1], "phone": [1, 1]})
        self.assertEqual((r["tp"], r["redactions"]), (3, 4))  # the visit date is not in the truth
        leaky = "Pt Ann [NAME], DOB [DATE], phone 202-555-0101, seen 2026-06-01."
        r2 = M.score_redaction(raw, leaky, truth)
        s = M.redaction_summary([r, r2])
        self.assertEqual(s["recall_by_type"], {"dob": 1.0, "name": 0.5, "phone": 0.5})
        self.assertEqual(s["leaked"], 2)
        self.assertEqual(s["precision"], round(5 / 6, 4))

    def test_reconcile_score(self):
        p = {"at_risk": True, "policy_doc_id": "POL-1", "policy_section": "Sec 4.2", "policy_effective_date": "2026-08-15"}
        self.assertEqual(M.score_reconcile(p, {"at_risk": True, "doc_id": "POL-1", "section": "Sec 4.2"},
                                           {"doc_id": "POL-1", "effective_date": "2026-08-15"}),
                         {"at_risk": True, "policy": True, "policy_version": True})
        self.assertEqual(M.score_reconcile(p, {"at_risk": True, "doc_id": "POL-0", "section": "Sec 4.2"},
                                           {"doc_id": "POL-0", "effective_date": "2026-06-01"}),
                         {"at_risk": True, "policy": False, "policy_version": False})
        self.assertEqual(M.score_reconcile({"at_risk": False}, {"at_risk": False}), {"at_risk": True})
        self.assertEqual(M.rate([{"a": True}, {"a": False}, {"b": 1}], "a"), 0.5)

    def test_retrieval_hit(self):
        hits = [["SOP-1-B", "Sec 4.2", "2026-06-01"], ["FIN-LAB", "Sec 2.3", "2026-06-01"]]
        self.assertEqual(M.retrieval_hit(hits, [["FIN-LAB", "Sec 2.3"]]), (True, 1.0, False))
        self.assertEqual(M.retrieval_hit(hits, [["FIN-LAB", "Sec 9"]]), (False, 0.0, False))
        self.assertEqual(M.retrieval_hit(hits, [["FIN-LAB", None], ["HR-DUTY", "Sec 2.1"]]), (True, 0.5, False))
        self.assertEqual(M.retrieval_hit(hits, [], forbid=["SOP-1-A"]), (True, 1.0, False))
        self.assertEqual(M.retrieval_hit(hits + [["SOP-1-A", "Sec 4.2", "x"]], [["SOP-1-B", "Sec 4.2"]],
                                         forbid=["SOP-1-A"]), (False, 1.0, True))
        self.assertEqual(M.retrieval_hit(hits, [], expect_none=True)[0], False)
        self.assertEqual(M.retrieval_hit([], [], expect_none=True)[0], True)
        far = [["X", "s", "d"]] * 5 + [["FIN-LAB", "Sec 2.3", "d"]]
        self.assertFalse(M.retrieval_hit(far, [["FIN-LAB", "Sec 2.3"]])[0])  # rank 6 is not top 5

    def test_numeric_correctness(self):
        nums = C.numbers("It missed 3 transports, $22,200 lost, at a 22.7% rate; plan missed by $22,200.")
        self.assertEqual(M.numeric_correctness(nums, [3, 22200, 0.2273]), 1.0)
        self.assertEqual(M.numeric_correctness(nums, [3, 1092]), 0.5)
        self.assertEqual(M.numeric_correctness(nums, [-22200]), 1.0)  # a shortfall may be written unsigned
        self.assertIsNone(M.numeric_correctness(nums, []))

    def test_parse_providers(self):
        self.assertEqual(R.parse_providers(["wave1.billing=local"]), {"wave1.billing": "local", "answer": "local"})
        self.assertEqual(R.parse_providers(["claude", "answer=rules"]), {"wave1.billing": "claude", "answer": "rules"})
        with self.assertRaises(SystemExit):
            R.parse_providers(["wave1.billing=gpt"])


class TestQualityWriteBack(unittest.TestCase):
    def test_apply_marks_only_matching_lines(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"IPA_ROUTING_LOG": os.path.join(td, "r.jsonl")}):
            log.append("other.task", "rules", "rules", 1, 1, 1, True)
            with score.Span() as s:
                log.append("wave1.billing.extract", "local", "m", 5, 10, 10, True)
                log.append("wave1.billing.extract", "local", "m", 5, 10, 10, False, error="x")
            self.assertEqual(list(s.lines), [1, 2])
            n = score.apply([(1, "wave1.billing.extract", 0.8), (2, "wave1.billing.extract", 0.0),
                             (0, "wave1.billing.extract", 1.0), (9, "x", 1.0)])
            self.assertEqual(n, 2)
            rows = log.read()
            self.assertEqual([r["quality"] for r in rows], [None, 0.8, 0.0])


def add_truth(con):
    """Tiny truth tables for the fixture (what data_gen writes for a real split)."""
    con.execute("""CREATE TABLE bronze.truth_epcrs (doc_id VARCHAR, transport_id VARCHAR, diagnosis VARCHAR,
                   instability_evidence VARCHAR, physician_certification BOOLEAN, loaded_miles INTEGER,
                   has_contraindication BOOLEAN, contraindication VARCHAR)""")
    con.executemany("INSERT INTO bronze.truth_epcrs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
        ("EP-1", "TR-2026-000001", "STEMI", "recurrent VT on monitor", True, 88, True,
         "Repeat VT made the unmonitored stretch of the road transfer unsafe."),
        ("EP-4", "TR-2026-000004", "shock", "MAP 52 on repeat cuff", False, 51, True,
         "Persistent hypotension ruled out the prolonged trip by ambulance.")])
    con.execute("CREATE TABLE bronze.truth_phi (doc_id VARCHAR, phi_type VARCHAR, value VARCHAR, span_start INTEGER, span_end INTEGER)")
    raw = dict(con.execute("SELECT doc_id, raw_text FROM bronze.epcrs").fetchall())
    rows = []
    for d, k, v in B.PHI:
        if d in ("EP-1", "EP-4"):
            i = raw[d].find(v)
            rows.append((d, k, v, i, i + len(v)))
    con.executemany("INSERT INTO bronze.truth_phi VALUES (?, ?, ?, ?, ?)", rows)
    con.execute("CREATE TABLE bronze.truth_claim_risk AS SELECT claim_id, at_risk, reason, policy_doc_id AS doc_id, "
                "policy_section AS section FROM silver.claim_reconciliation")
    con.execute("""CREATE TABLE bronze.truth_aog_impacts AS SELECT mx_id, 'RQ' AS request_id, base_id, tail_number,
                   CAST(date_trunc('month', start_ts) AS DATE) AS month, CAST(est_lost_transports AS INTEGER) AS lost_transports,
                   CAST(est_lost_revenue AS INTEGER) AS lost_revenue FROM silver.aog_impact""")


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import duckdb
        from waves import pipeline
        from waves.framework import Ctx
        cls.td = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {"IPA_ROUTING_LOG": os.path.join(cls.td.name, "r.jsonl"),
                                               "IPA_EMBED_CACHE": os.path.join(cls.td.name, "emb.duckdb")})
        cls.env.start()
        cls.db = os.path.join(cls.td.name, "w.duckdb")
        con = duckdb.connect(cls.db)
        B.build(con)
        pipeline.run(con, Ctx())
        add_truth(con)
        con.close()
        cls.qs = {"retrieval": [
            {"id": "r1", "q": "minimum observation time rotor restart", "role": "ops", "gold": [["SOP-1-B", "Sec 4.2"]],
             "forbid": ["SOP-1-A"]},
            {"id": "r2", "q": "recovery overtime hourly rate", "role": "finance", "gold": [["FIN-LAB", "Sec 2.3"]]},
            {"id": "r3", "q": "observation time", "role": "ops", "as_of": "2026-05-01", "expect_none": True}],
            "answers": [{"id": "a1", "q": "What did tail N412AM's AOG in September cost us?", "role": "exec",
                         "kind": "cross_department", "expect_numbers": [3, 22200], "expect_docs": ["FIN-REV"]},
                        {"id": "a2", "q": "What did tail N412AM's AOG in September cost us?", "role": "billing",
                         "expect_refusal": True}]}
        fake_embed = FakeEmbed()
        cls.emb = mock.patch("search.embed.Embedder.__init__",
                             lambda self, *a, **k: _init_fake(self, fake_embed, cls.td.name))
        cls.emb.start()

    @classmethod
    def tearDownClass(cls):
        cls.emb.stop()
        cls.env.stop()
        cls.td.cleanup()

    def run_eval(self, provider="rules", fake=None):
        out = os.path.join(self.td.name, "results")
        ctx = mock.patch.dict(P.PROVIDERS, {provider: fake}) if fake else mock.patch.dict(P.PROVIDERS, {})
        with ctx:
            return R.run("default", R.parse_providers([f"wave1.billing={provider}"]), limit=None, db=self.db,
                         answers_limit=None, out_dir=out, echo=lambda *_: None, questions=self.qs)

    def test_rules_end_to_end(self):
        r = self.run_eval()
        self.assertEqual(r["extraction"]["charts"], 2)
        self.assertEqual(r["extraction"]["overall"], 1.0)
        self.assertEqual(r["redaction"]["recall_by_type"]["ssn"], 1.0)
        self.assertEqual(r["reconciliation"]["at_risk_accuracy"], 1.0)
        self.assertEqual(r["retrieval"]["hit@5"], 1.0)
        self.assertEqual(r["retrieval"]["superseded_returned"], 0)
        self.assertEqual(set(r["retrieval"]["modes"]), {"hybrid"})  # 3 listed + questions derived from truth
        self.assertEqual(r["answers"]["refusal_accuracy"], 1.0)
        self.assertEqual(r["answers"]["groundedness"], 1.0)
        self.assertEqual(r["answers"]["numeric_correctness"], 1.0)
        saved = json.loads(open(r["path"]).read())
        self.assertEqual(saved["split"], "default")
        self.assertTrue(r["path"].endswith("-default-rules.json"))
        q = [x for x in log.read() if x["task"] == "wave1.billing.extract" and x["provider"] == "rules"]
        self.assertTrue(q and all(x["quality"] == 1.0 for x in q[-2:]))

    def test_model_provider_scored_and_quality_written(self):
        # a fake cloud model: extraction JSON with a wrong diagnosis; answers with an invented number
        class FakeCloud(FakeWriter):
            def __call__(self, prompt, model, json_mode=False, **kw):
                if "Extract fields" in prompt:
                    self.prompts.append(prompt)
                    return json.dumps({"diagnosis": "unknown", "instability_evidence": None,
                                       "ground_contraindication": None, "physician_certification": True,
                                       "loaded_miles": None}), {}
                return super().__call__(prompt, model, json_mode, **kw)
        fake = FakeCloud("The AOG cost $1 [1].")
        r = self.run_eval("claude", fake)
        self.assertLess(r["extraction"]["overall"], 0.5)
        self.assertEqual(r["redaction"]["names_provider"], "rules")  # raw PHI never goes to claude
        for p in fake.prompts:
            self.assertEqual(__import__("llm").find_phi(p), [])
        self.assertEqual(r["answers"]["numeric_correctness"], 0.0)  # refused after the post-check
        rows = [x for x in log.read() if x["provider"] == "claude"]
        ex = [x for x in rows if x["task"] == "wave1.billing.extract"]
        self.assertTrue(ex and all(x["quality"] is not None and x["quality"] < 0.5 for x in ex[-2:]))
        self.assertTrue(any(x["task"] == "answer.write" and x["quality"] is not None for x in rows))


def _init_fake(self, fn, td):
    self.url, self.model, self.fn, self.timeout = "fake://", "fake", fn, 1
    from pathlib import Path
    self.cache = Path(td) / "emb.duckdb"
    self.stats = {"hits": 0, "computed": 0, "ms": 0.0}
    self._up = True


if __name__ == "__main__":
    unittest.main()
