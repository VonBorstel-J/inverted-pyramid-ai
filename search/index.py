"""gold.search_chunks: the hybrid index rows (ARCHITECTURE.md section 4, gold pool).

One row per ACTIVE document section (from silver.doc_sections, which already
drops SUPERSEDED documents) plus one row per de-identified ePCR narrative
(silver.epcr_deid; never bronze raw text). Each row carries the citation
triple [doc_id, section, effective_date] and a chunk_hash (sha256 of the text
that is embedded), the key of the embedding cache.

    python -m search.index [--db warehouse.duckdb] [--embed] [--narratives]
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from waves.framework import fetch, has_table

REPO = Path(__file__).resolve().parent.parent

DOC_SQL = """SELECT doc_id || '#' || section AS chunk_id, 'doc' AS kind, doc_id, dept, title, section,
                    status, CAST(effective_date AS DATE) AS effective_date, text, CAST(NULL AS VARCHAR) AS transport_id
             FROM silver.doc_sections WHERE status = 'ACTIVE'"""
NARR_SQL = """SELECT e.doc_id || '#narrative' AS chunk_id, 'narrative' AS kind, e.doc_id, 'BILLING' AS dept,
                     'ePCR narrative ' || e.transport_id AS title, 'narrative' AS section, 'ACTIVE' AS status,
                     CAST({date} AS DATE) AS effective_date, e.deid_text AS text, e.transport_id
              FROM silver.epcr_deid e {join}"""


def embed_text(row):
    """The exact text that is indexed and embedded for a chunk."""
    if row["kind"] == "doc":
        return f"{row['title']} | {row['section']} | {row['text']}"
    return row["text"]


def chunk_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _select(con):
    parts = []
    if has_table(con, "silver.doc_sections"):
        parts.append(DOC_SQL)
    if has_table(con, "silver.epcr_deid"):
        if has_table(con, "bronze.transports"):
            parts.append(NARR_SQL.format(date="t.date", join="LEFT JOIN bronze.transports t USING (transport_id)"))
        else:
            parts.append(NARR_SQL.format(date="NULL", join=""))
    return " UNION ALL ".join(parts)


def _with_hash(rows):
    for r in rows:
        r["chunk_hash"] = chunk_hash(embed_text(r))
    return rows


def build(con):
    """(Re)create gold.search_chunks. Returns the row count."""
    con.execute("CREATE SCHEMA IF NOT EXISTS gold")
    sel = _select(con)
    rows = _with_hash(fetch(con, sel + " ORDER BY 1")) if sel else []
    con.execute("""CREATE OR REPLACE TABLE gold.search_chunks (
        chunk_id VARCHAR, kind VARCHAR, doc_id VARCHAR, dept VARCHAR, title VARCHAR, section VARCHAR,
        status VARCHAR, effective_date DATE, text VARCHAR, transport_id VARCHAR, chunk_hash VARCHAR)""")
    if rows:
        cols = ["chunk_id", "kind", "doc_id", "dept", "title", "section", "status", "effective_date", "text",
                "transport_id", "chunk_hash"]
        con.executemany(f"INSERT INTO gold.search_chunks VALUES ({', '.join('?' * len(cols))})",
                        [[r[c] for c in cols] for r in rows])
    return len(rows)


def load(con):
    """All chunks as dicts: from gold.search_chunks when built, else computed
    on the fly from silver (works on a read-only connection)."""
    if has_table(con, "gold.search_chunks"):
        return fetch(con, "SELECT * FROM gold.search_chunks WHERE status = 'ACTIVE' ORDER BY chunk_id")
    sel = _select(con)
    return _with_hash(fetch(con, f"SELECT * FROM ({sel}) WHERE status = 'ACTIVE' ORDER BY chunk_id")) if sel else []


def main(argv=None):
    import duckdb
    from search import embed

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(REPO / "warehouse.duckdb"))
    ap.add_argument("--embed", action="store_true", help="embed uncached chunks through the embedding server")
    ap.add_argument("--narratives", action="store_true", help="also embed the de-identified narratives")
    a = ap.parse_args(argv)
    con = duckdb.connect(a.db)
    try:
        n = build(con)
        out = {"chunks": n}
        if a.embed:
            rows = [r for r in load(con) if a.narratives or r["kind"] == "doc"]
            e = embed.Embedder()
            if not e.available():
                print(json.dumps({**out, "embedded": 0, "note": f"embedding server down at {e.url}"}))
                return 1
            got = e.embed_docs([embed_text(r) for r in rows])
            out.update(embedded=sum(v is not None for v in got), cached_before=e.stats["hits"],
                       computed=e.stats["computed"])
        print(json.dumps(out))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
