-- One schema per role. Raw PHI columns (bronze.epcrs.raw_text) appear ONLY in
-- role_billing_phi. Every other role sees de-identified or aggregated data.
-- This mirrors Snowflake masking + row access policies (docs/SNOWFLAKE.md).

-- ---------------------------------------------------------------- billing_phi
CREATE SCHEMA IF NOT EXISTS role_billing_phi;
CREATE OR REPLACE VIEW role_billing_phi.epcrs AS SELECT doc_id, transport_id, raw_text FROM bronze.epcrs;
CREATE OR REPLACE VIEW role_billing_phi.epcr_deid AS SELECT doc_id, transport_id, deid_text, n_redactions, redaction_types FROM silver.epcr_deid;
CREATE OR REPLACE VIEW role_billing_phi.transports AS SELECT * FROM bronze.transports;
CREATE OR REPLACE VIEW role_billing_phi.claims AS SELECT * FROM bronze.claims;
CREATE OR REPLACE VIEW role_billing_phi.epcr_extract AS SELECT * FROM silver.epcr_extract;
CREATE OR REPLACE VIEW role_billing_phi.claim_reconciliation AS SELECT * FROM silver.claim_reconciliation;

-- ---------------------------------------------------------------- billing
CREATE SCHEMA IF NOT EXISTS role_billing;
CREATE OR REPLACE VIEW role_billing.epcr_deid AS SELECT doc_id, transport_id, deid_text, n_redactions, redaction_types FROM silver.epcr_deid;
CREATE OR REPLACE VIEW role_billing.transports AS
    SELECT transport_id, base_id, tail_number, date, payer, cpt_code, loaded_miles, icd10_code,
           billed_amount, collected_amount, claim_status FROM bronze.transports;
CREATE OR REPLACE VIEW role_billing.claims AS
    SELECT claim_id, transport_id, payer, submitted_date, status, denial_code, paid_amount FROM bronze.claims;
CREATE OR REPLACE VIEW role_billing.epcr_extract AS
    SELECT doc_id, transport_id, diagnosis, instability_evidence, ground_contraindication, physician_certification,
           loaded_miles, prov_provider, prov_model, prov_prompt_version FROM silver.epcr_extract;
CREATE OR REPLACE VIEW role_billing.claim_reconciliation AS SELECT * FROM silver.claim_reconciliation;
CREATE OR REPLACE VIEW role_billing.payer_policies AS SELECT * FROM silver.doc_sections WHERE dept = 'BILLING';
CREATE OR REPLACE VIEW role_billing.claims_at_risk_by_base_month AS SELECT * FROM gold.claims_at_risk_by_base_month;
CREATE OR REPLACE VIEW role_billing.claims_at_risk_by_reason AS SELECT * FROM gold.claims_at_risk_by_reason;

-- ---------------------------------------------------------------- ops
CREATE SCHEMA IF NOT EXISTS role_ops;
CREATE OR REPLACE VIEW role_ops.aircraft AS SELECT * FROM bronze.aircraft;
CREATE OR REPLACE VIEW role_ops.dispatch_log AS SELECT * FROM bronze.dispatch_log;
CREATE OR REPLACE VIEW role_ops.maintenance AS
    SELECT m.mx_id, m.tail_number, m.base_id, m."start", m."end", m.aog_hours, m.issue_text, m.missed_requests,
           c.issue_category, c.system, c.severity
    FROM bronze.maintenance_events m LEFT JOIN silver.mx_classified c USING (mx_id);
CREATE OR REPLACE VIEW role_ops.aog_impact AS SELECT * FROM silver.aog_impact;
CREATE OR REPLACE VIEW role_ops.aog_events AS SELECT * FROM gold.aog_events;
CREATE OR REPLACE VIEW role_ops.sops AS SELECT * FROM silver.doc_sections WHERE dept = 'OPS';

-- ---------------------------------------------------------------- finance
CREATE SCHEMA IF NOT EXISTS role_finance;
CREATE OR REPLACE VIEW role_finance.ledger_lines AS SELECT * FROM silver.ledger_lines;
CREATE OR REPLACE VIEW role_finance.variance_drivers AS SELECT * FROM gold.variance_drivers;
CREATE OR REPLACE VIEW role_finance.revenue_by_base_month AS SELECT * FROM gold.revenue_by_base_month;
CREATE OR REPLACE VIEW role_finance.leases AS SELECT * FROM bronze.leases;
CREATE OR REPLACE VIEW role_finance.aog_events AS SELECT * FROM gold.aog_events;
CREATE OR REPLACE VIEW role_finance.policies AS SELECT * FROM silver.doc_sections WHERE dept = 'FINANCE';

-- ---------------------------------------------------------------- hr
CREATE SCHEMA IF NOT EXISTS role_hr;
CREATE OR REPLACE VIEW role_hr.crew AS SELECT crew_id, base_id, role, hire_date FROM bronze.crew;
CREATE OR REPLACE VIEW role_hr.license_status AS SELECT * FROM silver.license_status;
CREATE OR REPLACE VIEW role_hr.shifts AS SELECT * FROM bronze.shifts;
CREATE OR REPLACE VIEW role_hr.overtime_causes AS SELECT * FROM silver.overtime_causes;
CREATE OR REPLACE VIEW role_hr.compliance_flags AS SELECT * FROM silver.hr_compliance_flags;
CREATE OR REPLACE VIEW role_hr.aog_overtime AS SELECT * FROM silver.aog_overtime;
CREATE OR REPLACE VIEW role_hr.handbook AS SELECT * FROM silver.doc_sections WHERE dept = 'HR';

-- ---------------------------------------------------------------- exec (aggregates only)
CREATE SCHEMA IF NOT EXISTS role_exec;
CREATE OR REPLACE VIEW role_exec.claims_at_risk_by_base_month AS SELECT * FROM gold.claims_at_risk_by_base_month;
CREATE OR REPLACE VIEW role_exec.aog_events AS
    SELECT mx_id, tail_number, base_id, month, aog_hours, system, issue_category, est_lost_transports,
           est_lost_revenue, value_basis, overtime_hours, overtime_cost, claims_at_risk_same_base_month,
           billed_at_risk_same_base_month, est_total_cost FROM gold.aog_events;
CREATE OR REPLACE VIEW role_exec.revenue_by_base_month AS SELECT * FROM gold.revenue_by_base_month;
CREATE OR REPLACE VIEW role_exec.variance_drivers AS SELECT * FROM gold.variance_drivers;
CREATE OR REPLACE VIEW role_exec.overtime_by_cause AS SELECT * FROM gold.overtime_by_cause;
CREATE OR REPLACE VIEW role_exec.compliance_by_base_month AS SELECT * FROM gold.compliance_by_base_month;
CREATE OR REPLACE VIEW role_exec.license_status_by_base AS SELECT * FROM gold.license_status_by_base;
CREATE OR REPLACE VIEW role_exec.metric_catalog AS SELECT * FROM gold.metric_catalog;
CREATE OR REPLACE VIEW role_exec.active_documents AS
    SELECT doc_id, dept, title, effective_date, section, text FROM silver.doc_sections;
