"""Router, PHI guard, JSON retry, routing log, report. No network, no models."""
import json
import os
import tempfile
import unittest
from unittest import mock

from pydantic import BaseModel

import llm
from llm import local_server, log, phi, report
from llm import providers as P


class Out(BaseModel):
    category: str
    severity: str


class FakeProvider:
    """Scripted replies; records prompts."""
    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def __call__(self, prompt, model, json_mode=False, **_):
        self.prompts.append(prompt)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r, {"tokens_in": 10, "tokens_out": 5}


class RouterCase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.logp = os.path.join(self.td.name, "routing.jsonl")
        self.env = mock.patch.dict(os.environ, {"IPA_ROUTING_LOG": self.logp})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.td.cleanup()

    def rows(self):
        return log.read(self.logp)

    def fake(self, name, *replies):
        f = FakeProvider(*replies)
        p = mock.patch.dict(P.PROVIDERS, {name: f})
        p.start()
        self.addCleanup(p.stop)
        return f


class TestRouter(RouterCase):
    def test_rules_passthrough_and_log_fields(self):
        out = llm.call("wave1.ops.classify", "hydraulic leak", provider="rules", rules=lambda p: p.upper())
        self.assertEqual(out, "HYDRAULIC LEAK")
        (r,) = self.rows()
        for k in ("ts", "task", "provider", "model", "ms", "input_chars", "output_chars",
                  "tokens_in", "tokens_out", "ok", "error", "quality"):
            self.assertIn(k, r)
        self.assertEqual((r["task"], r["provider"], r["ok"]), ("wave1.ops.classify", "rules", True))
        self.assertEqual(r["input_chars"], len("hydraulic leak"))

    def test_fake_cloud_provider_logged_with_tokens(self):
        self.fake("claude", "hello")
        self.assertEqual(llm.call("t", "clean text", provider="claude"), "hello")
        (r,) = self.rows()
        self.assertEqual((r["provider"], r["tokens_in"], r["tokens_out"], r["output_chars"]), ("claude", 10, 5, 5))

    def test_provider_error_logged_not_ok(self):
        self.fake("local", P.ProviderError("down"))
        with self.assertRaises(P.ProviderError):
            llm.call("t", "x", provider="local")
        (r,) = self.rows()
        self.assertFalse(r["ok"])
        self.assertIn("down", r["error"])

    def test_unknown_provider(self):
        with self.assertRaises(llm.RouterError):
            llm.call("t", "x", provider="nope")


class TestPHIGuard(RouterCase):
    PHI = [
        "Patient SSN 123-45-6789 on file",
        "MRN: 00482913 noted",
        "call back at (303) 555-0142",
        "callback 720-555-0199",
        "DOB: 04/12/1961",
        "date of birth 1961-04-12",
        "seen 4/12/1961",
    ]

    def test_patterns_detected(self):
        for t in self.PHI:
            self.assertTrue(phi.find_phi(t), t)

    def test_clean_text_passes(self):
        for t in ["Pt [NAME] DOB [DATE] SSN [SSN]", "loaded miles 42, BP 80/50, GCS 9",
                  "transport TR-2026-000123 tail N412AM effective 2026-07-01"]:
            self.assertEqual(phi.find_phi(t), [], t)

    def test_cloud_providers_refused_and_never_called(self):
        for prov in ("claude", "codex", "gemini"):
            f = self.fake(prov, "should not be used")
            for t in self.PHI:
                with self.assertRaises(llm.PHIGuardError):
                    llm.call("wave1.billing.extract", t, provider=prov)
            self.assertEqual(f.prompts, [])
        rows = self.rows()
        self.assertEqual(len(rows), 3 * len(self.PHI))
        self.assertTrue(all(not r["ok"] and "phi_guard" in r["error"] for r in rows))

    def test_local_and_rules_allowed(self):
        f = self.fake("local", "ok")
        self.assertEqual(llm.call("t", self.PHI[0], provider="local"), "ok")
        self.assertEqual(llm.call("t", self.PHI[0], provider="rules", rules=lambda p: "r"), "r")
        self.assertEqual(len(f.prompts), 1)

    def test_guard_in_json_mode(self):
        self.fake("gemini", '{"category":"a","severity":"b"}')
        with self.assertRaises(llm.PHIGuardError):
            llm.call("t", "SSN 123-45-6789", provider="gemini", schema=Out)


class TestJSONMode(RouterCase):
    def test_valid_first_time(self):
        self.fake("local", '```json\n{"category": "unscheduled", "severity": "high"}\n```')
        r = llm.call("t", "classify", provider="local", schema=Out)
        self.assertEqual(r, Out(category="unscheduled", severity="high"))
        self.assertEqual(len(self.rows()), 1)

    def test_retry_once_with_validation_error(self):
        f = self.fake("claude", '{"category": "x"}', 'Sure! {"category": "x", "severity": "low"}')
        r = llm.call("t", "classify", provider="claude", schema=Out)
        self.assertEqual(r.severity, "low")
        self.assertEqual(len(f.prompts), 2)
        self.assertIn("rejected", f.prompts[1])
        self.assertIn("severity", f.prompts[1])
        self.assertEqual([x["ok"] for x in self.rows()], [False, True])

    def test_gives_up_after_one_retry(self):
        f = self.fake("codex", "not json", '{"category": 1}', "unused")
        with self.assertRaises(llm.RouterError):
            llm.call("t", "classify", provider="codex", schema=Out)
        self.assertEqual(len(f.prompts), 2)
        self.assertEqual(len(self.rows()), 2)

    def test_rules_returns_dict(self):
        r = llm.call("t", "x", provider="rules", schema=Out,
                     rules=lambda p: {"category": "c", "severity": "s"})
        self.assertIsInstance(r, Out)


class TestReport(RouterCase):
    def test_summary(self):
        for ms, ok, q in [(10, True, 0.9), (30, True, None), (20, False, 0.5)]:
            log.append("wave1.billing.extract", "local", "m", ms, 1, 1, ok, quality=q)
        log.append("wave1.billing.extract", "claude", "m", 99, 1, 1, True)
        s = report.summarize(self.rows())
        loc = next(x for x in s if x["provider"] == "local")
        self.assertEqual((loc["count"], loc["median_ms"], loc["success_rate"], loc["quality"]), (3, 20, 0.667, 0.7))
        self.assertIn("wave1.billing.extract", report.format_table(s))
        with mock.patch("sys.stdout"):
            self.assertEqual(report.main(["--log", self.logp]), 0)


class TestLocalServer(unittest.TestCase):
    def test_command_shape(self):
        c = local_server.build_cmd("embed", 8092, embedding=True)
        self.assertIn("--embedding", c)
        self.assertEqual(c[c.index("-t") + 1], "4")
        self.assertEqual(c[c.index("-c") + 1], "4096")
        self.assertTrue(c[2].endswith("embeddinggemma-300m-q8_0.gguf"))

    def test_health_down_without_server(self):
        self.assertFalse(local_server.health(1, timeout=0.2))

    def test_local_provider_payload(self):
        class R:
            def raise_for_status(self): pass
            def json(self): return {"choices": [{"message": {"content": "hi"}}],
                                    "usage": {"prompt_tokens": 3, "completion_tokens": 1}}
        with mock.patch("requests.post", return_value=R()) as post:
            text, meta = P.local("p", "m", json_mode=True, url="http://x/v1")
        self.assertEqual((text, meta["tokens_in"]), ("hi", 3))
        self.assertEqual(post.call_args[0][0], "http://x/v1/chat/completions")
        self.assertEqual(post.call_args[1]["json"]["response_format"], {"type": "json_object"})


if __name__ == "__main__":
    unittest.main()
