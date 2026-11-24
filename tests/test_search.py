"""search/: RRF fusion, ACTIVE-only and as_of scoping, role scoping, the
BM25-only fallback, and the chunk-hash embedding cache. Fake embeddings only
(no network, no model)."""
import datetime as dt
import hashlib
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import bronze_fixture as B  # noqa: E402

from llm import providers as P  # noqa: E402
from search import hybrid, index, rrf, search  # noqa: E402
from search.embed import Embedder  # noqa: E402
from waves import pipeline  # noqa: E402
from waves.framework import Ctx  # noqa: E402

DIM = 64


class FakeEmbed:
    """Deterministic bag-of-words hashing 'embedding'; counts calls."""
    def __init__(self):
        self.calls, self.texts = 0, 0

    def __call__(self, texts):
        self.calls += 1
        self.texts += len(texts)
        out = []
        for t in texts:
            v = np.zeros(DIM)
            body = t.split("query: ", 1)[-1].split("text: ", 1)[-1]
            for w in hybrid.tokenize(body):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1
            out.append(v.tolist())
        return out


def fixture():
    con = B.build()
    with mock.patch.dict(P.PROVIDERS, {"local": B.FakeNER()}):
        pipeline.run(con, Ctx(providers={"wave1.billing.redact_names": "local"}))
    return con


class TestSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {"IPA_ROUTING_LOG": os.path.join(cls.td.name, "r.jsonl"),
                                               "IPA_EMBED_CACHE": os.path.join(cls.td.name, "emb.duckdb")})
        cls.env.start()
        cls.con = fixture()

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.env.stop()
        cls.td.cleanup()

    def emb(self):
        return Embedder(fn=FakeEmbed(), cache=os.path.join(self.td.name, "emb.duckdb"))

    # ------------------------------------------------------------ fusion
    def test_rrf_math(self):
        got = rrf({"bm25": ["x", "y"], "vector": ["y", "z"]}, k=60)
        self.assertEqual([g[0] for g in got], ["y", "x", "z"])
        self.assertAlmostEqual(got[0][1], 1 / 61 + 1 / 62)
        self.assertAlmostEqual(got[1][1], 1 / 61)
        self.assertEqual(got[0][2], {"bm25": 2, "vector": 1})

    def test_rrf_ties_break_by_id(self):
        got = rrf({"a": ["q"], "b": ["p"]})
        self.assertEqual([g[0] for g in got], ["p", "q"])

    def test_hybrid_mode_uses_both_rankers(self):
        r = search(self.con, "minimum observation time rotor restart", role="ops", as_of="2026-09-01",
                   embedder=self.emb())
        self.assertEqual(r.mode, "hybrid")
        self.assertEqual(r.hits[0].doc_id, "SOP-1-B")
        self.assertIn("vector", r.hits[0].ranks)
        self.assertIn("bm25", r.hits[0].ranks)

    # ------------------------------------------------------------ scope
    def test_chunks_are_active_only(self):
        statuses = {r[0] for r in self.con.execute("SELECT DISTINCT status FROM gold.search_chunks").fetchall()}
        self.assertEqual(statuses, {"ACTIVE"})
        ids = {r[0] for r in self.con.execute("SELECT doc_id FROM gold.search_chunks").fetchall()}
        self.assertNotIn("SOP-1-A", ids)
        self.assertNotIn("POL-CM-0", ids)

    def test_superseded_never_returned(self):
        e = self.emb()
        for q in ("minimum observation time 15 minutes", "legacy physician certification Sec 2.1",
                  "Cairnvale Mutual transport policy (legacy)", "rotor restart inspection"):
            for role in ("exec", "ops", "billing"):
                for as_of in ("2026-07-15", "2026-10-01", None):
                    r = search(self.con, q, role=role, as_of=as_of, k=10, embedder=e)
                    self.assertFalse({"SOP-1-A", "POL-CM-0"} & {h.doc_id for h in r.hits}, (q, role, as_of))

    def test_as_of_filters_and_keeps_latest_version(self):
        e = self.emb()
        q = "Cairnvale Mutual transport policy loaded miles physician certification"
        before = {h.doc_id for h in search(self.con, q, role="billing", as_of="2026-07-01", k=10, embedder=e).hits}
        after = {h.doc_id for h in search(self.con, q, role="billing", as_of="2026-09-01", k=10, embedder=e).hits}
        self.assertIn("POL-CM-1", before)
        self.assertNotIn("POL-CM-2", before)  # not yet effective
        self.assertIn("POL-CM-2", after)
        self.assertNotIn("POL-CM-1", after)  # replaced by the revision in effect
        none = search(self.con, "minimum observation time", role="ops", as_of="2026-05-01", embedder=e)
        self.assertEqual(none.hits, [])  # SOP-1-B not effective yet, SOP-1-A superseded

    def test_series_key(self):
        self.assertEqual(hybrid.series("Cairnvale Mutual transport policy rev 2"), "cairnvale mutual transport policy")
        self.assertEqual(hybrid.series("Cairnvale Mutual transport policy (legacy)"), "cairnvale mutual transport policy")

    def test_role_scoping(self):
        e = self.emb()
        q = "ground transport physician certification observation overtime duty chart"
        ops = search(self.con, q, role="ops", k=20, embedder=e)
        self.assertTrue(ops.hits and all(h.dept == "OPS" and h.kind == "doc" for h in ops.hits))
        ex = search(self.con, "Wristband STEMI VT road transfer unsafe", role="exec", k=20, embedder=e)
        self.assertFalse([h for h in ex.hits if h.kind == "narrative"])
        bl = search(self.con, "Wristband STEMI VT road transfer unsafe", role="billing", k=20, embedder=e)
        self.assertTrue([h for h in bl.hits if h.kind == "narrative"])
        with self.assertRaises(PermissionError):
            search(self.con, "x", role="nobody", embedder=e)

    def test_narratives_are_deidentified(self):
        bl = search(self.con, "Wristband reads patient name", role="billing", k=20, embedder=self.emb())
        text = " ".join(h.text for h in bl.hits)
        for v in B.phi_values():
            self.assertNotRegex(text, r"(?<![\w])" + re.escape(v) + r"(?![\w])")

    def test_every_hit_carries_citation(self):
        r = search(self.con, "recovery overtime rate per hour", role="exec", embedder=self.emb())
        self.assertTrue(r.hits)
        for h in r.hits:
            self.assertEqual(len(h.citation), 3)
            dt.date.fromisoformat(h.citation[2])
        self.assertEqual(r.hits[0].citation, ["FIN-LAB", "Sec 2.3", "2026-06-01"])

    # ------------------------------------------------------------ fallback + cache
    def test_bm25_only_fallback_says_so(self):
        down = Embedder(url="http://127.0.0.1:9/v1", cache=os.path.join(self.td.name, "empty.duckdb"))
        r = search(self.con, "duty limit per shift", role="hr", embedder=down)
        self.assertEqual(r.mode, "bm25-only")
        self.assertTrue(any("BM25 only" in n for n in r.notes))
        self.assertEqual(r.hits[0].doc_id, "HR-DUTY")

    def test_cache_keyed_by_chunk_hash(self):
        cache = os.path.join(self.td.name, "c2.duckdb")
        fake = FakeEmbed()
        e = Embedder(fn=fake, cache=cache)
        rows = index.load(self.con)
        texts = [index.embed_text(r) for r in rows]
        e.embed_docs(texts)
        n = fake.texts
        self.assertEqual(n, len(set(texts)))
        e2 = Embedder(fn=fake, cache=cache)
        e2.embed_docs(texts)
        self.assertEqual(fake.texts, n)  # all served from the cache
        self.assertEqual(e2.stats["hits"], len(texts))
        import duckdb
        c = duckdb.connect(cache, read_only=True)
        keys = {r[0] for r in c.execute("SELECT hash FROM embeddings").fetchall()}
        c.close()
        self.assertTrue({r["chunk_hash"] for r in rows} <= keys)
        # a cached query vector works with the server down
        e2.embed_query("duty limit per shift")
        off = Embedder(url="http://127.0.0.1:9/v1", cache=cache)
        r = search(self.con, "duty limit per shift", role="hr", embedder=off)
        self.assertEqual(r.mode, "hybrid")


if __name__ == "__main__":
    unittest.main()
