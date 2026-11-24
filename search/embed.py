"""Embeddings from EmbeddingGemma through llama-server's OpenAI-compatible
/v1/embeddings (start it with `python -m llm.local_server start --model embed
--port 8092`), cached in DuckDB keyed by chunk hash.

The cache lives in its own DuckDB file (runs/embed_cache.duckdb, git-ignored)
so it survives a regenerated warehouse and is shared by the default and hard
splits: a key is sha256 of the embedded text, so a changed chunk is a new key.
Query embeddings are cached the same way, which lets a later run reuse them
with no server up.
"""
import os
import time
from pathlib import Path

import numpy as np

from search.index import chunk_hash

REPO = Path(__file__).resolve().parent.parent
DEFAULT_URL = os.environ.get("IPA_EMBED_URL", "http://127.0.0.1:8092/v1")
MODEL = os.environ.get("IPA_EMBED_MODEL", "embeddinggemma-300m-q8_0")
BATCH = 8

# EmbeddingGemma's retrieval prompts
QUERY_FMT = "task: search result | query: {}"
DOC_FMT = "title: none | text: {}"


def cache_path():
    return Path(os.environ.get("IPA_EMBED_CACHE", REPO / "runs" / "embed_cache.duckdb"))


class Embedder:
    """`fn` (list[str] -> list[list[float]]) replaces the HTTP server; tests
    pass a fake. `available()` says whether new texts can be embedded."""

    def __init__(self, url=None, model=MODEL, fn=None, cache=None, timeout=120):
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.model = model
        self.fn = fn
        self.cache = Path(cache) if cache else cache_path()
        self.timeout = timeout
        self.stats = {"hits": 0, "computed": 0, "ms": 0.0}
        self._up = None

    # ------------------------------------------------------------ server
    def available(self):
        if self.fn is not None:
            return True
        if self._up is None:
            import requests
            try:
                r = requests.get(self.url.rsplit("/v1", 1)[0] + "/health", timeout=1.5)
                self._up = r.status_code == 200
            except Exception:
                self._up = False
        return self._up

    def _compute(self, texts):
        if self.fn is not None:
            return self.fn(texts)
        import requests
        out = []
        for i in range(0, len(texts), BATCH):
            r = requests.post(self.url + "/embeddings", json={"input": texts[i:i + BATCH], "model": self.model},
                              timeout=self.timeout)
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
            out.extend(d["embedding"] for d in data)
        return out

    # ------------------------------------------------------------ cache
    def _con(self, read_only=False):
        import duckdb
        p = self.cache
        if read_only and not p.exists():
            return None
        p.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(p), read_only=read_only)
        if not read_only:
            con.execute("""CREATE TABLE IF NOT EXISTS embeddings (
                hash VARCHAR, model VARCHAR, dim INTEGER, vec FLOAT[], created TIMESTAMP DEFAULT current_timestamp,
                PRIMARY KEY (hash, model))""")
        return con

    def _get(self, hashes):
        if not hashes:
            return {}
        try:
            con = self._con(read_only=True)
        except Exception:
            con = None
        if con is None:
            return {}
        try:
            if not con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='embeddings'").fetchone()[0]:
                return {}
            rows = con.execute("SELECT hash, vec FROM embeddings WHERE model = ? AND hash IN (SELECT unnest(?))",
                               [self.model, list(hashes)]).fetchall()
            return {h: np.asarray(v, dtype=np.float32) for h, v in rows}
        finally:
            con.close()

    def _put(self, items):
        if not items:
            return
        con = self._con()
        try:
            con.executemany("INSERT OR REPLACE INTO embeddings (hash, model, dim, vec) VALUES (?, ?, ?, ?)",
                            [[h, self.model, len(v), [float(x) for x in v]] for h, v in items])
        finally:
            con.close()

    # ------------------------------------------------------------ public
    def _embed(self, keys, texts, compute=True):
        """[np.ndarray (unit length) or None] per (key, text); None when
        uncached and the server is down (or compute is False)."""
        got = self._get(set(keys))
        self.stats["hits"] += sum(1 for k in keys if k in got)
        missing = [i for i, k in enumerate(keys) if k not in got]
        missing_keys = list(dict.fromkeys(keys[i] for i in missing))
        if missing_keys and compute and self.available():
            first = {}
            for i in missing:
                first.setdefault(keys[i], texts[i])
            t0 = time.perf_counter()
            vecs = self._compute([first[k] for k in missing_keys])
            self.stats["ms"] += (time.perf_counter() - t0) * 1000
            self.stats["computed"] += len(vecs)
            new = list(zip(missing_keys, vecs))
            self._put(new)
            got.update({k: np.asarray(v, dtype=np.float32) for k, v in new})
        out = []
        for k in keys:
            v = got.get(k)
            if v is not None:
                n = float(np.linalg.norm(v))
                v = v / n if n else v
            out.append(v)
        return out

    def embed_docs(self, texts, compute=True):
        """Key = chunk_hash(text), the same value as gold.search_chunks.chunk_hash."""
        return self._embed([chunk_hash(t) for t in texts], [DOC_FMT.format(t) for t in texts], compute)

    def embed_query(self, text, compute=True):
        return self._embed([chunk_hash("query\n" + text)], [QUERY_FMT.format(text)], compute)[0]
