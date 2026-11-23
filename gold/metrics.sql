-- Governed metric definitions: the one place a metric is defined.
-- Every number here is computed in SQL from silver/bronze; models never compute numbers.
CREATE SCHEMA IF NOT EXISTS gold;

-- Billing: claims at risk of denial, per base and month.
CREATE OR REPLACE VIEW gold.claims_at_risk_by_base_month AS
SELECT t.base_id,
       CAST(date_trunc('month', r.transport_date) AS DATE) AS month,
       count(*)                                       AS claims,
       count(*) FILTER (WHERE r.at_risk)              AS claims_at_risk,
       round(count(*) FILTER (WHERE r.at_risk) / count(*), 4) AS at_risk_rate,
       coalesce(sum(t.billed_amount) FILTER (WHERE r.at_risk), 0) AS billed_at_risk
FROM silver.claim_reconciliation r
JOIN bronze.transports t USING (transport_id)
GROUP BY ALL;

-- Billing: at-risk claims by reason and the policy cited.
CREATE OR REPLACE VIEW gold.claims_at_risk_by_reason AS
SELECT reason, policy_doc_id, policy_section, policy_effective_date, count(*) AS claims
FROM silver.claim_reconciliation
WHERE at_risk
GROUP BY ALL;

-- Ops x Finance x HR x Billing: one row per AOG event with its cross-department cost.
CREATE OR REPLACE VIEW gold.aog_events AS
WITH ot AS (
    SELECT mx_id, sum(overtime_hours) AS overtime_hours, sum(overtime_cost) AS overtime_cost
    FROM silver.aog_overtime GROUP BY mx_id),
claims_win AS (
    SELECT a.mx_id,
           count(r.claim_id)                          AS claims_same_base_month,
           count(r.claim_id) FILTER (WHERE r.at_risk) AS claims_at_risk_same_base_month,
           coalesce(sum(t.billed_amount) FILTER (WHERE r.at_risk), 0) AS billed_at_risk_same_base_month
    FROM silver.aog_impact a
    LEFT JOIN bronze.transports t
      ON t.base_id = a.base_id
     AND date_trunc('month', CAST(t.date AS DATE)) = date_trunc('month', a.start_ts)
    LEFT JOIN silver.claim_reconciliation r ON r.transport_id = t.transport_id
    GROUP BY a.mx_id)
SELECT a.mx_id, a.tail_number, a.base_id,
       CAST(date_trunc('month', a.start_ts) AS DATE) AS month,
       a.start_ts, a.end_ts, a.aog_hours,
       c.system, c.issue_category, c.severity,
       a.est_lost_transports, a.value_per_lost_transport, a.value_basis, a.est_lost_revenue,
       coalesce(ot.overtime_hours, 0) AS overtime_hours,
       ot.overtime_cost,
       cw.claims_same_base_month, cw.claims_at_risk_same_base_month, cw.billed_at_risk_same_base_month,
       coalesce(a.est_lost_revenue, 0) + coalesce(ot.overtime_cost, 0) AS est_total_cost
FROM silver.aog_impact a
LEFT JOIN silver.mx_classified c USING (mx_id)
LEFT JOIN ot USING (mx_id)
LEFT JOIN claims_win cw USING (mx_id);

-- Finance: revenue vs plan per base and month.
CREATE OR REPLACE VIEW gold.revenue_by_base_month AS
SELECT base_id, month, actual AS revenue, planned AS revenue_planned, actual - planned AS revenue_variance
FROM silver.ledger_lines WHERE line = 'revenue';

-- Finance: every line that missed plan, with its driver.
CREATE OR REPLACE VIEW gold.variance_drivers AS
SELECT base_id, month, line, line_type, actual, planned, variance, variance_pct, favorable, driver, driver_value
FROM silver.finance_variance;

-- HR: overtime hours by cause per base and month.
CREATE OR REPLACE VIEW gold.overtime_by_cause AS
SELECT c.base_id, CAST(date_trunc('month', o.date) AS DATE) AS month, o.cause,
       sum(o.overtime_hours) AS overtime_hours, count(*) AS shifts
FROM silver.overtime_causes o
LEFT JOIN bronze.crew c USING (crew_id)
GROUP BY ALL;

-- HR: compliance flags per base and month.
CREATE OR REPLACE VIEW gold.compliance_by_base_month AS
SELECT base_id, CAST(date_trunc('month', date) AS DATE) AS month, flag, count(*) AS flags
FROM silver.hr_compliance_flags
GROUP BY ALL;

-- HR: license expiry status counts per base.
CREATE OR REPLACE VIEW gold.license_status_by_base AS
SELECT c.base_id, l.status, count(*) AS licenses, count(*) FILTER (WHERE l.expires_in_window) AS expiring_in_window
FROM silver.license_status l
LEFT JOIN bronze.crew c USING (crew_id)
GROUP BY ALL;

-- The catalog of governed metrics (what the question-answering layer may cite).
CREATE OR REPLACE VIEW gold.metric_catalog AS
SELECT * FROM (VALUES
  ('claims_at_risk_rate', 'gold.claims_at_risk_by_base_month', 'at-risk claims / all claims, by transport month and base'),
  ('billed_at_risk', 'gold.claims_at_risk_by_base_month', 'billed amount of claims failing the payer policy in effect on the transport date'),
  ('est_lost_transports', 'gold.aog_events', 'declined_aog dispatch requests logged against the AOG event (else missed_requests)'),
  ('est_lost_revenue', 'gold.aog_events', 'est_lost_transports x value per lost transport from the ACTIVE finance policy'),
  ('aog_overtime_cost', 'gold.aog_events', 'AOG-caused overtime hours x the policy overtime rate'),
  ('est_total_cost', 'gold.aog_events', 'est_lost_revenue + aog_overtime_cost'),
  ('revenue_variance', 'gold.revenue_by_base_month', 'actual revenue - planned revenue'),
  ('variance', 'gold.variance_drivers', 'actual - planned per ledger line'),
  ('overtime_hours', 'gold.overtime_by_cause', 'sum of overtime hours by extracted cause'),
  ('compliance_flags', 'gold.compliance_by_base_month', 'expired license on a flown shift; shift over the handbook duty limit')
) AS t(metric, view_name, definition);
