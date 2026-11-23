"""Finance waves (all SQL: models never compute numbers).

wave1 finance.normalize  bronze.base_ledger + budget -> silver.ledger_lines
wave2 finance.variance   silver.ledger_lines + ops/hr -> silver.finance_variance (with drivers)
"""
from waves.framework import columns, fetch, gate, has_table, provenance, step
from waves.schemas import LedgerLine, Variance

LINES = ("revenue", "fuel", "maintenance_cost", "crew_cost", "lease_cost")
# budget spellings seen in practice -> canonical ledger line
LINE_ALIASES = {"revenue": "revenue", "fuel": "fuel", "fuel_cost": "fuel", "maintenance": "maintenance_cost",
                "maintenance_cost": "maintenance_cost", "mx": "maintenance_cost", "crew": "crew_cost",
                "crew_cost": "crew_cost", "labor": "crew_cost", "lease": "lease_cost", "lease_cost": "lease_cost",
                "leases": "lease_cost"}


@step("finance.normalize", 1, "silver.ledger_lines")
def normalize_step(con, ctx, res):
    """Unpivot the ledger to one row per base, month, line; tag the budget line."""
    if not has_table(con, "bronze.base_ledger"):
        return gate(con, "silver.ledger_lines", LedgerLine, [], res, ctx.run_at)
    have = [c for c in LINES if c in columns(con, "bronze.base_ledger")]
    union = " UNION ALL ".join(
        f"SELECT base_id, CAST(date_trunc('month', CAST(month AS DATE)) AS DATE) AS month, '{c}' AS line, "
        f"CAST({c} AS DOUBLE) AS actual FROM bronze.base_ledger" for c in have)
    alias = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in LINE_ALIASES.items())
    budget = (f"""SELECT base_id, CAST(date_trunc('month', CAST(month AS DATE)) AS DATE) AS month,
                         CASE lower(trim(replace(line, ' ', '_'))) {alias} ELSE lower(trim(line)) END AS line,
                         sum(planned) AS planned
                  FROM bronze.budget GROUP BY ALL""" if has_table(con, "bronze.budget")
              else "SELECT NULL AS base_id, NULL::DATE AS month, NULL AS line, NULL::DOUBLE AS planned")
    rows = fetch(con, f"""
        WITH a AS ({union}), b AS ({budget})
        SELECT a.base_id, a.month, a.line,
               CASE WHEN a.line = 'revenue' THEN 'revenue' ELSE 'cost' END AS line_type,
               a.actual, CAST(b.planned AS DOUBLE) AS planned
        FROM a LEFT JOIN b USING (base_id, month, line)
        ORDER BY a.base_id, a.month, a.line""")
    res.rows_in, res.provider = len(rows), "sql"
    out = [(r, provenance(f"{r['base_id']},{r['month']},{r['line']}", 1, "finance.normalize", "sql", "sql",
                          "ledger.v1")) for r in rows]
    gate(con, "silver.ledger_lines", LedgerLine, out, res, ctx.run_at)


def _driver_sql(con):
    """Per base/month driver figures from ops and hr, computed in SQL."""
    parts = []
    if has_table(con, "silver.aog_impact"):
        parts.append("""SELECT base_id, CAST(date_trunc('month', start_ts) AS DATE) AS month,
                               sum(est_lost_transports) AS aog_lost, sum(aog_hours) AS aog_hours, 0 AS ot_hours
                        FROM silver.aog_impact GROUP BY ALL""")
    if has_table(con, "silver.aog_overtime"):
        parts.append("""SELECT base_id, CAST(date_trunc('month', date) AS DATE) AS month,
                               0, 0, sum(overtime_hours) FROM silver.aog_overtime GROUP BY ALL""")
    if not parts:
        return "SELECT NULL AS base_id, NULL::DATE AS month, 0 AS aog_lost, 0 AS aog_hours, 0 AS ot_hours"
    return ("SELECT base_id, month, sum(aog_lost) AS aog_lost, sum(aog_hours) AS aog_hours, sum(ot_hours) AS ot_hours "
            f"FROM ({' UNION ALL '.join(parts)}) GROUP BY ALL")


@step("finance.variance", 2, "silver.finance_variance")
def variance_step(con, ctx, res):
    """Variance vs plan for every line that misses plan, with its operational driver."""
    if not has_table(con, "silver.ledger_lines"):
        return gate(con, "silver.finance_variance", Variance, [], res, ctx.run_at)
    rows = fetch(con, f"""
        WITH d AS ({_driver_sql(con)})
        SELECT l.base_id, l.month, l.line, l.line_type, l.actual, l.planned,
               l.actual - l.planned AS variance,
               CASE WHEN l.planned <> 0 THEN round((l.actual - l.planned) / abs(l.planned), 4) END AS variance_pct,
               CASE WHEN l.line_type = 'revenue' THEN l.actual >= l.planned ELSE l.actual <= l.planned END AS favorable,
               CASE
                 WHEN l.line = 'revenue' AND coalesce(d.aog_lost, 0) > 0 THEN 'AOG missed requests'
                 WHEN l.line = 'crew_cost' AND coalesce(d.ot_hours, 0) > 0 THEN 'AOG recovery overtime'
                 WHEN l.line = 'maintenance_cost' AND coalesce(d.aog_hours, 0) > 0 THEN 'AOG repairs'
                 ELSE 'no operational driver found' END AS driver,
               CASE
                 WHEN l.line = 'revenue' AND coalesce(d.aog_lost, 0) > 0 THEN d.aog_lost
                 WHEN l.line = 'crew_cost' AND coalesce(d.ot_hours, 0) > 0 THEN d.ot_hours
                 WHEN l.line = 'maintenance_cost' AND coalesce(d.aog_hours, 0) > 0 THEN d.aog_hours END AS driver_value
        FROM silver.ledger_lines l LEFT JOIN d USING (base_id, month)
        WHERE l.planned IS NOT NULL AND l.actual <> l.planned
        ORDER BY l.base_id, l.month, l.line""")
    res.rows_in, res.provider = len(rows), "sql"
    out = [(r, provenance(f"{r['base_id']},{r['month']},{r['line']}", 2, "finance.variance", "sql", "sql",
                          "variance.v1")) for r in rows]
    gate(con, "silver.finance_variance", Variance, out, res, ctx.run_at)


STEPS = [normalize_step, variance_step]
