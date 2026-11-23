"""Build the gold pool: gold.entity_links (the entity graph) and the governed
metric views in gold/metrics.sql."""
from pathlib import Path

from waves.framework import columns, has_table

HERE = Path(__file__).resolve().parent

# (required tables, SELECT src_type, src_id, rel, dst_type, dst_id)
LINKS = [
    (("bronze.transports",), "SELECT 'transport', transport_id, 'flown_by', 'tail', tail_number FROM bronze.transports WHERE tail_number IS NOT NULL"),
    (("bronze.transports",), "SELECT 'transport', transport_id, 'from_base', 'base', base_id FROM bronze.transports"),
    (("bronze.claims",), "SELECT 'claim', claim_id, 'bills', 'transport', transport_id FROM bronze.claims"),
    (("bronze.epcrs",), "SELECT 'epcr', doc_id, 'documents', 'transport', transport_id FROM bronze.epcrs"),
    (("bronze.aircraft",), "SELECT 'tail', tail_number, 'based_at', 'base', base_id FROM bronze.aircraft"),
    (("bronze.maintenance_events",), "SELECT 'mx', mx_id, 'grounded', 'tail', tail_number FROM bronze.maintenance_events"),
    (("bronze.crew",), "SELECT 'crew', crew_id, 'based_at', 'base', base_id FROM bronze.crew"),
    (("silver.aog_overtime",), "SELECT DISTINCT 'crew', crew_id, 'overtime_for', 'mx', mx_id FROM silver.aog_overtime"),
    (("silver.claim_reconciliation",), "SELECT 'claim', claim_id, 'judged_by', 'doc', policy_doc_id FROM silver.claim_reconciliation WHERE policy_doc_id IS NOT NULL"),
]


def build_entity_links(con):
    parts = [sql for need, sql in LINKS if all(has_table(con, t) for t in need)]
    if has_table(con, "bronze.dispatch_log") and "mx_id" in columns(con, "bronze.dispatch_log"):
        parts.append("SELECT 'request', request_id, 'declined_for', 'mx', mx_id FROM bronze.dispatch_log WHERE mx_id IS NOT NULL")
    body = " UNION ALL ".join(parts) or "SELECT NULL, NULL, NULL, NULL, NULL WHERE FALSE"
    con.execute("CREATE SCHEMA IF NOT EXISTS gold")
    con.execute(f"""CREATE OR REPLACE TABLE gold.entity_links AS
                    SELECT DISTINCT CAST(c1 AS VARCHAR) AS src_type, CAST(c2 AS VARCHAR) AS src_id, CAST(c3 AS VARCHAR) AS rel,
                           CAST(c4 AS VARCHAR) AS dst_type, CAST(c5 AS VARCHAR) AS dst_id
                    FROM ({body}) AS u(c1, c2, c3, c4, c5)""")
    return con.execute("SELECT count(*) FROM gold.entity_links").fetchone()[0]


def build_metrics(con):
    con.execute((HERE / "metrics.sql").read_text())


def build(con):
    n = build_entity_links(con)
    build_metrics(con)
    return n
