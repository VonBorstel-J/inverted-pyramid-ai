"""Hybrid retrieval with citations (ARCHITECTURE.md sections 2 and 4).

    from search import search
    res = search(con, "minimum observation time after rotor restart", role="ops", as_of="2026-09-01")
    res.mode            # "hybrid" or "bm25-only" (says why in res.notes)
    res.hits[0].citation  # [doc_id, section, effective_date]
"""
from search.hybrid import Hit, SearchResult, rrf, scope, search

__all__ = ["search", "Hit", "SearchResult", "rrf", "scope"]
