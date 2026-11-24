"""answer/: governed numbers, the numeric and citation post-checks, the
refusal path, role scoping. Fake providers and fake embeddings only."""
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import bronze_fixture as B  # noqa: E402
from test_search import FakeEmbed, fixture  # noqa: E402

from answer import REFUSAL, ask  # noqa: E402
from answer import check as C  # noqa: E402
from answer import plan as PL  # noqa: E402
from llm import log  # noqa: E402
from llm import providers as P  # noqa: E402
from llm.phi import find_phi  # noqa: E402
from search.embed import Embedder  # noqa: E402

SHOWCASE = "What did tail N412AM's AOG in September cost us?"


class FakeWriter:
    """Fake model: classification JSON from the rules, then scripted answers
    (one per call; the last repeats). Records every prompt."""
    def __init__(self, *answers, kind=None):
        self.answers, self.kind, self.prompts = list(answers), kind, []

    def __call__(self, prompt, model, json_mode=False, **_):
        self.prompts.append(prompt)
        if json_mode:
            q = re.search(r"Question: (.*)", prompt).group(1)
            return json.dumps({"kind": self.kind or PL.rules_classify(q)["kind"]}), {}
        return (self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]), {}


class TestAnswer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {"IPA_ROUTING_LOG": os.path.join(cls.td.name, "r.jsonl"),
                                               "IPA_EMBED_CACHE": os.path.join(cls.td.name, "emb.duckdb")})
        cls.env.start()
        cls.con = fixture()
        cls.emb = Embedder(fn=FakeEmbed(), cache=os.path.join(cls.td.name, "emb.duckdb"))

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.env.stop()
        cls.td.cleanup()

    def ask(self, q, role="exec", provider="rules", **kw):
        return ask(q, role, con=self.con, embedder=self.emb, provider=provider, **kw)

    def with_fake(self, fake, q, role="exec", provider="local", **kw):
        with mock.patch.dict(P.PROVIDERS, {provider: fake}):
            return self.ask(q, role, provider=provider, **kw)

    # ------------------------------------------------------------ the showcase
    def test_showcase_rules(self):
        a = self.ask(SHOWCASE)
        self.assertFalse(a.refused, a.reason)
        self.assertEqual(a.kind, "cross_department")
        self.assertIn("$22,200", a.answer)       # 3 declined_aog x $7400 from FIN-REV Sec 1.1
        self.assertIn("3 transports", a.answer)
        cites = [c[0] for c in a.citations]
        self.assertIn("gold.aog_events", cites)
        self.assertIn("FIN-REV", cites)
        self.assertIn("FIN-LAB", cites)
        self.assertTrue(a.checks["post_check"]["ok"])
        self.assertEqual(str(a.as_of), "2026-09-30")

    def test_numbers_come_from_sql(self):
        a = self.ask(SHOWCASE)
        row = self.con.execute("SELECT est_lost_revenue, overtime_cost, est_total_cost FROM gold.aog_events "
                               "WHERE tail_number = 'N412AM'").fetchone()
        for v in row:
            self.assertIn(f"${v:,.0f}", a.answer)

    def test_model_answer_accepted_when_grounded(self):
        fake = FakeWriter("Tail N412AM missed 3 transports, an estimated $22,200 in lost revenue [1]. "
                          "Lost revenue uses $7400 per missed AOG request [2].")
        a = self.with_fake(fake, SHOWCASE)
        self.assertFalse(a.refused, a.reason)
        self.assertEqual(a.cited, [1, 2])
        self.assertEqual(a.attempts, 1)

    # ------------------------------------------------------------ post-checks
    def test_numeric_post_check_refuses_invented_number(self):
        fake = FakeWriter("The AOG cost us $99,999 in lost revenue [1].")
        a = self.with_fake(fake, SHOWCASE)
        self.assertTrue(a.refused)
        self.assertEqual(a.answer, REFUSAL)
        self.assertIn("99,999", a.reason)
        self.assertEqual(a.attempts, 2)  # one retry with the problems, then refuse
        self.assertIn("rejected by the checker", fake.prompts[-1])

    def test_numeric_post_check_retry_recovers(self):
        total = self.con.execute("SELECT est_total_cost FROM gold.aog_events WHERE tail_number = 'N412AM'").fetchone()[0]
        fake = FakeWriter("It cost $25,000 [1].", f"It cost an estimated ${total:,.0f} in total [1].")
        a = self.with_fake(fake, SHOWCASE)
        self.assertFalse(a.refused, a.reason)
        self.assertEqual(a.attempts, 2)

    def test_model_computed_sum_is_rejected(self):
        # 22,200 + 1,092 + 135,752 is not a governed number: the model may not add
        a = self.with_fake(FakeWriter("All in, it cost $159,044 [1]."), SHOWCASE)
        self.assertTrue(a.refused)

    def test_citation_post_check(self):
        a = self.with_fake(FakeWriter("Lost revenue was $22,200 [9]."), SHOWCASE)
        self.assertTrue(a.refused)
        self.assertIn("[9]", a.reason)
        a = self.with_fake(FakeWriter("The minimum observation time is 15 minutes [SOP-1-A, Sec 4.2, 2026-04-01]."),
                           "For rotor restart inspection, what is the minimum observation time?", role="ops")
        self.assertTrue(a.refused)
        self.assertIn("not in the retrieved set", a.reason)

    def test_uncited_number_rejected(self):
        a = self.with_fake(FakeWriter("Lost revenue was $22,200. Overtime cost $1,092 [1]."), SHOWCASE)
        self.assertTrue(a.refused)
        self.assertIn("uncited number", a.reason)

    def test_full_form_citation_accepted(self):
        a = self.with_fake(FakeWriter("The minimum observation time is 30 minutes [SOP-1-B, Sec 4.2, 2026-06-01]."),
                           "For rotor restart inspection, what is the minimum observation time?", role="ops")
        self.assertFalse(a.refused, a.reason)
        self.assertEqual(a.citations, [["SOP-1-B", "Sec 4.2", "2026-06-01"]])

    def test_check_unit(self):
        srcs = [{"n": 1, "kind": "sql", "citation": ["gold.v", "k", "2026-09-30"], "text": ""}]
        rows = [{"at_risk_rate": 0.2273, "claims": 44, "billed": 135752.0}]
        self.assertTrue(C.check("22.73% of 44 claims, $135,752 [1].", srcs, rows)["ok"])
        self.assertTrue(C.check("About 22.7% were at risk [1].", srcs, rows)["ok"])
        self.assertFalse(C.check("About 23.5% were at risk [1].", srcs, rows)["ok"])
        self.assertFalse(C.check("$135,000 billed [1].", srcs, rows)["ok"])
        # identifiers are not numbers
        self.assertEqual(C.numbers("N412AM at DEN-01, MX-2026-000001, Sec 4.2, CO-50"), [])
        self.assertEqual([n[1] for n in C.numbers("$1,092 and 3 and 14.5%")], [1092.0, 3.0, 14.5])
        self.assertFalse(C.check("Nothing cited.", srcs, rows)["ok"])

    # ------------------------------------------------------------ refusal + roles
    def test_refuses_when_role_lacks_the_view(self):
        a = self.ask(SHOWCASE, role="billing")  # role_billing has no aog_events
        self.assertTrue(a.refused)
        self.assertEqual(a.answer, REFUSAL)

    def test_refuses_without_any_source(self):
        a = self.ask("What does the policy say about quantum teleportation?", role="hr", as_of="2026-01-01")
        self.assertTrue(a.refused)

    def test_writer_refusal_is_a_refusal(self):
        a = self.with_fake(FakeWriter(REFUSAL), SHOWCASE)
        self.assertTrue(a.refused)
        self.assertEqual(a.answer, REFUSAL)

    def test_exec_gets_no_identifiers(self):
        for q in (SHOWCASE, "How many claims were at risk at DEN-01 in September?",
                  "How much overtime did DEN-01 log in September?", "What does the chart say about the patient?"):
            a = self.ask(q, role="exec")
            text = a.render() + json.dumps(a.to_dict(), default=str)
            self.assertEqual(find_phi(text), [], q)
            self.assertNotRegex(text, r"\b(TR|CL|CR|EP)-\d|\bEP-\d", q)
            for v in B.phi_values():
                self.assertNotRegex(text, r"(?<![\w])" + re.escape(v) + r"(?![\w])", q)
            self.assertFalse([s for s in a.sources if s.get("chunk_id", "").endswith("#narrative")])

    def test_cloud_writer_gets_deidentified_text_only(self):
        fake = FakeWriter("The chart states a ground contraindication [1].")
        a = self.with_fake(fake, "Which chart says the road transfer was unsafe for the patient?", role="billing",
                           provider="claude")
        self.assertTrue(fake.prompts)
        for p in fake.prompts:
            self.assertEqual(find_phi(p), [])
            for v in B.phi_values():
                self.assertNotRegex(p, r"(?<![\w])" + re.escape(v) + r"(?![\w])")
        self.assertFalse(a.refused, a.reason)

    def test_metric_question_role_scoped(self):
        a = self.ask("How many claims were at risk at DEN-01 in September?", role="billing")
        self.assertFalse(a.refused, a.reason)
        self.assertEqual(a.kind, "metric")
        self.assertEqual(a.sources[0]["citation"][0], "gold.claims_at_risk_by_base_month")
        b = self.ask("Why were claims at risk?", role="exec")  # role_exec has no claims_at_risk_by_reason
        self.assertTrue(b.refused)

    # ------------------------------------------------------------ classification
    def test_rules_classify(self):
        self.assertEqual(PL.rules_classify(SHOWCASE)["kind"], "cross_department")
        self.assertEqual(PL.rules_classify("How many claims were at risk in September?")["kind"], "metric")
        self.assertEqual(PL.rules_classify("What is the minimum observation time after a rotor restart?")["kind"],
                         "document")

    def test_model_metric_without_rows_falls_back_to_rules_kind(self):
        fake = FakeWriter("Duty may not exceed 14 hours per shift [1].", kind="metric")
        a = self.with_fake(fake, "What is the maximum duty length per shift?", role="hr")
        self.assertEqual(a.kind, "document")
        self.assertIn("overrode local", a.checks["classified_by"])
        self.assertFalse(a.refused, a.reason)
        b = self.with_fake(FakeWriter("x", kind="metric"), SHOWCASE, role="billing")
        self.assertTrue(b.refused)  # rules also says cross-department: still a refusal

    def test_model_classify_logged(self):
        fake = FakeWriter("x", kind="metric")
        with mock.patch.dict(P.PROVIDERS, {"local": fake}):
            kind, by = PL.classify("anything at all", "local")
        self.assertEqual((kind, by), ("metric", "local"))
        rows = [r for r in log.read() if r["task"] == "answer.classify" and r["provider"] == "local"]
        self.assertTrue(rows and rows[-1]["ok"])

    def test_entities_and_as_of(self):
        e = PL.entities(SHOWCASE)
        self.assertEqual((e.tail, str(e.month)), ("N412AM", "2026-09-01"))
        self.assertEqual(str(PL.default_as_of(e)), "2026-09-30")
        self.assertIsNone(PL.entities("Duty may not exceed 14 hours").month)


if __name__ == "__main__":
    unittest.main()
