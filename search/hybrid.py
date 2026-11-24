"""Hybrid retrieval over gold.search_chunks: BM25 (rank-bm25) + EmbeddingGemma
vectors, fused with reciprocal rank fusion (RRF). With no embedding server
(and no cached query vector) it runs BM25 only and says so in `mode`/`notes`.

Scope, applied before ranking:
- ACTIVE chunks only (SUPERSEDED never enter gold.search_chunks, and the filter
  is applied again here);
- effective_date <= as_of; when a document series has several versions in
  effect (a policy and its revision), only the latest one by as_of is kept;
- the role's departments, mirroring governance/views.sql; de-identified
  narratives only for billing roles.
Every hit carries the citation [doc_id, section, effective_date].
"""
import datetime as dt
import re
from dataclasses import dataclass, field

import numpy as np
from rank_bm25 import BM25Okapi

from search import index

RRF_K = 60
ALL_DEPTS = frozenset({"BILLING", "OPS", "FINANCE", "HR"})
# documents each role may read (governance/views.sql: payer_policies, sops, policies, handbook, active_documents)
ROLE_DEPTS = {"billing_phi": {"BILLING"}, "billing": {"BILLING"}, "ops": {"OPS"}, "finance": {"FINANCE"},
              "hr": {"HR"}, "exec": ALL_DEPTS}
NARRATIVE_ROLES = frozenset({"billing_phi", "billing"})  # role_*.epcr_deid
STOP = frozenset("a an and are as at be by did does do for from has have how i in is it its of on or our s that the "
                 "their this to us was we were what when where which who why will with".split())
_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)*")
_SERIES = re.compile(r"\s*(?:\((?:legacy|rev[^)]*|v\d+[^)]*|revised[^)]*)\)|\b(?:rev(?:ision)?|v|version)\.?\s*\d+\b|\brevised\b).*$", re.I)


class BM25(BM25Okapi):
    """BM25Okapi with Lucene's always-positive IDF, log(1 + (N - n + 0.5) / (n + 0.5)):
    Okapi's IDF is zero or negative for a term in half the corpus, which breaks
    the small role-scoped pools (two HR sections, three payer policies)."""

    def _calc_idf(self, nd):
        import math
        for word, freq in nd.items():
            self.idf[word] = math.log(1 + (self.corpus_size - freq + 0.5) / (freq + 0.5))


def tokenize(text):
    return [t for t in _TOKEN.findall((text or "").lower()) if t not in STOP]


def series(title):
    """Document series key: the title without revision markers."""
    return _SERIES.sub("", (title or "").strip()).strip().lower()


@dataclass
class Hit:
    chunk_id: str
    kind: str
    doc_id: str
    dept: str
    title: str
    section: str
    effective_date: object
    text: str
    score: float = 0.0
    ranks: dict = field(default_factory=dict)

    @property
    def citation(self):
        d = self.effective_date
        return [self.doc_id, self.section, d.isoformat() if hasattr(d, "isoformat") else (str(d) if d else None)]

    def to_dict(self):
        return {"chunk_id": self.chunk_id, "kind": self.kind, "citation": self.citation, "title": self.title,
                "dept": self.dept, "score": round(self.score, 6), "ranks": self.ranks, "text": self.text}


@dataclass
class SearchResult:
    query: str
    hits: list
    mode: str  # "hybrid" | "bm25-only"
    notes: list = field(default_factory=list)
    as_of: object = None
    role: str = None

    @property
    def citations(self):
        return [h.citation for h in self.hits]


def rrf(rankings, k=RRF_K):
    """Reciprocal rank fusion. rankings: {name: [id in rank order]}.
    Returns [(id, score, {name: rank})] best first; ties broken by id."""
    scores, ranks = {}, {}
    for name, ids in rankings.items():
        for r, i in enumerate(ids, 1):
            scores[i] = scores.get(i, 0.0) + 1.0 / (k + r)
            ranks.setdefault(i, {})[name] = r
    return sorted(((i, s, ranks[i]) for i, s in scores.items()), key=lambda x: (-x[1], x[0]))


def _as_date(v):
    if v is None or isinstance(v, dt.date) and not isinstance(v, dt.datetime):
        return v
    if isinstance(v, dt.datetime):
        return v.date()
    return dt.date.fromisoformat(str(v)[:10])


def scope(chunks, role, as_of, kinds=None):
    """Filter chunks to what `role` may see and what is in effect on as_of."""
    if role not in ROLE_DEPTS:
        raise PermissionError(f"unknown role {role!r}")
    depts = ROLE_DEPTS[role]
    as_of = _as_date(as_of)
    out = []
    for c in chunks:
        if c.get("status") != "ACTIVE":
            continue
        if kinds and c["kind"] not in kinds:
            continue
        if c["kind"] == "narrative":
            if role not in NARRATIVE_ROLES:
                continue
        elif c["dept"] not in depts:
            continue
        eff = _as_date(c.get("effective_date"))
        if as_of is not None and (eff is None or eff > as_of):
            continue
        out.append(c)
    # latest version per document series (documents only)
    latest = {}
    for c in out:
        if c["kind"] == "doc":
            key = (c["dept"], series(c["title"]))
            eff = _as_date(c["effective_date"])
            if key not in latest or (eff, c["doc_id"]) > latest[key]:
                latest[key] = (eff, c["doc_id"])
    return [c for c in out if c["kind"] != "doc"
            or latest[(c["dept"], series(c["title"]))][1] == c["doc_id"]]


def bm25_rank(chunks, query, n=50):
    if not chunks:
        return []
    docs = [tokenize(index.embed_text(c)) for c in chunks]
    q = tokenize(query)
    if not q:
        return []
    bm = BM25(docs)
    scores = bm.get_scores(q)
    order = sorted(range(len(chunks)), key=lambda i: (-scores[i], chunks[i]["chunk_id"]))
    qs = set(q)
    return [chunks[i]["chunk_id"] for i in order[:n] if qs & set(docs[i])]


def vector_rank(chunks, qvec, vecs, n=50):
    have = [(c["chunk_id"], v) for c, v in zip(chunks, vecs) if v is not None]
    if qvec is None or not have:
        return []
    m = np.stack([v for _, v in have])
    sims = m @ qvec
    order = sorted(range(len(have)), key=lambda i: (-float(sims[i]), have[i][0]))
    return [have[i][0] for i in order[:n]]


def search(con, query, role="exec", as_of=None, k=5, embedder=None, kinds=None, chunks=None):
    """Top-k hits for `query` as `role` on `as_of` (default today)."""
    as_of = _as_date(as_of) or dt.date.today()
    pool = scope(chunks if chunks is not None else index.load(con), role, as_of, kinds)
    notes = []
    rankings = {"bm25": bm25_rank(pool, query)}
    mode = "bm25-only"
    if embedder is None:
        from search.embed import Embedder
        embedder = Embedder()
    qvec = embedder.embed_query(query)
    if qvec is None:
        notes.append(f"BM25 only: no embedding server at {getattr(embedder, 'url', '?')} and no cached query vector")
    else:
        vecs = embedder.embed_docs([index.embed_text(c) for c in pool])
        missing = sum(v is None for v in vecs)
        if missing == len(vecs) and vecs:
            notes.append("BM25 only: no chunk vectors cached and no embedding server")
        else:
            rankings["vector"] = vector_rank(pool, qvec, vecs)
            mode = "hybrid"
            if missing:
                notes.append(f"vectors for {len(vecs) - missing}/{len(vecs)} chunks; the rest ranked by BM25 only")
    by_id = {c["chunk_id"]: c for c in pool}
    hits = []
    for cid, score, ranks in rrf(rankings)[:k]:
        c = by_id[cid]
        hits.append(Hit(cid, c["kind"], c["doc_id"], c["dept"], c["title"], c["section"], c["effective_date"],
                        c["text"], score, ranks))
    return SearchResult(query, hits, mode, notes, as_of, role)
