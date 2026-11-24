"""Local Streamlit console for the synthetic operations warehouse."""
import json
import sys
from pathlib import Path

import duckdb
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from answer import ask
from evals.run import summary_row
from governance.roles import query
from llm import log
from llm.report import summarize

# Each page has a fixed role and a fixed set of published views.
DEPARTMENTS = {
    "Billing": ("billing", "Review claims against the policy in effect on the flight date.",
                ["claims_at_risk_by_base_month", "claims_at_risk_by_reason", "claim_reconciliation", "payer_policies"],
                "How many claims were at risk at DEN-01 in September?"),
    "Flight operations": ("ops", "Trace aircraft downtime to missed transport requests.",
                          ["aog_events", "maintenance", "dispatch_log", "sops"],
                          "For rotor restart inspection, what is the current minimum observation time?"),
    "Finance": ("finance", "Compare revenue and spending with the base budget.",
                ["variance_drivers", "revenue_by_base_month", "ledger_lines", "leases"],
                "How far did DEN-01 revenue miss plan in September?"),
    "Crew": ("hr", "Review shift compliance, license status and the causes of overtime.",
             ["compliance_flags", "license_status", "overtime_causes", "aog_overtime"],
             "What is the maximum duty length per shift?"),
    "Across departments": ("exec", "Connect a maintenance event to the operating and financial records for its base.",
                           ["aog_events", "revenue_by_base_month", "overtime_by_cause", "claims_at_risk_by_base_month", "metric_catalog"],
                           "What did tail N412AM's AOG in September cost us?"),
}


def claim_lookup(db):
    with st.expander("Inspect a claim and its policy"):
        with st.form("claim-lookup"):
            claim = st.text_input("Claim ID", value="CL-2026-000598")
            submit = st.form_submit_button("Find claim")
        if submit:
            rows = query(db, "billing", """
                SELECT r.claim_id, r.transport_date, r.at_risk, r.reason, r.policy_doc_id,
                       r.policy_section, r.policy_effective_date, p.text AS policy_text
                FROM claim_reconciliation r
                LEFT JOIN payer_policies p
                    ON p.doc_id = r.policy_doc_id AND p.section = r.policy_section
                    AND p.effective_date = r.policy_effective_date
                WHERE r.claim_id = ?
            """, [claim.strip().upper()])
            if rows:
                st.json(rows[0], expanded=True)
            else:
                st.info("No claim matched that ID.")


def outage_lookup(db):
    events = query(db, "exec", "SELECT * FROM aog_events ORDER BY month, mx_id")
    if not events:
        return
    with st.expander("Trace the cost of an aircraft outage"):
        index = st.selectbox("Maintenance event", range(len(events)),
                             format_func=lambda i: f"{events[i]['mx_id']} · {events[i]['tail_number']}")
        event = events[index]
        for column, label, field in zip(st.columns(3),
                ["Estimated lost revenue", "Overtime cost", "Estimated total"],
                ["est_lost_revenue", "overtime_cost", "est_total_cost"]):
            column.metric(label, f"${event[field]:,.0f}" if event[field] is not None else "Unavailable")
        st.caption(f"Base {event['base_id']} · {event['aog_hours']} hours grounded · {event['value_basis']}")


def department(page):
    role, intro, views, example = DEPARTMENTS[page]
    st.write(intro)
    path = ROOT / "warehouse.duckdb"
    if not path.is_file():
        st.warning("Build the synthetic warehouse using the commands in the README.")
        return
    with duckdb.connect(str(path), read_only=True) as db:
        view = st.selectbox("Dataset", views, format_func=lambda v: v.replace("_", " ").capitalize(), key=f"dataset-{role}")
        columns = [row[0] for row in db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = ? AND table_name = ?",
            [f"role_{role}", view]).fetchall()]
        if not columns:
            st.warning("This dataset is missing. Run the pipeline to build its role view.")
            return
        where, params = "", []
        if "base_id" in columns:
            bases = [r["base_id"] for r in query(db, role, f"SELECT DISTINCT base_id FROM {view} ORDER BY base_id")]
            base = st.selectbox("Base", [None, *bases], format_func=lambda v: v or "All bases", key=f"base-{role}-{view}")
            if base is not None:
                where, params = " WHERE base_id = ?", [base]
        count = query(db, role, f"SELECT count(*) AS n FROM {view}{where}", params)[0]["n"]
        st.caption(f"{count:,} matching rows · Showing the first {min(count, 100):,} in sorted order")
        st.dataframe(query(db, role, f"SELECT * FROM {view}{where} ORDER BY ALL LIMIT 100", params),
                     hide_index=True, width="stretch")
        if role == "billing":
            claim_lookup(db)
        elif role == "exec":
            outage_lookup(db)
        with st.form(f"ask-{role}"):
            question = st.text_input("Ask a question about this department", value=example)
            submit = st.form_submit_button("Look up answer")
        if submit and question.strip():
            outcome = ask(question.strip(), role=role, provider="rules", con=db)
            st.text(outcome.render())
            if outcome.reason and outcome.refused:
                st.caption(outcome.reason)


def percent(value):
    return "Unavailable" if value is None else f"{value:.1%}"


def evaluations():
    st.write("Inspect saved runs. Each run records its chart sample, provider and scoring method.")
    results = []
    comparisons = {}
    for path in sorted((ROOT / "evals/results").glob("*.json"), reverse=True):
        try:
            result = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if "extraction" in result:
            results.append((path.stem, result))
        elif result.get("summary") and result.get("providers"):
            comparisons[path.stem] = result
    if comparisons:
        st.subheader("Provider comparison")
        comparison = comparisons[st.selectbox("Matched run", list(comparisons))]
        st.dataframe([{
            "Provider": row["provider"],
            "Charts": row["charts"],
            "Fields correct": percent(row.get("extract_acc")),
            "Claim risk correct": percent(row.get("reconcile")),
            "Identifiers removed": percent(row.get("redact_recall")),
        } for row in comparison["summary"]], hide_index=True, width="stretch")
        st.caption("Scores describe the code and providers used in this saved run.")
    if not results:
        st.info("Completed runs will appear here.")
        return
    runs = dict(results)
    result = runs[st.selectbox("Saved run", list(runs))]
    summary = summary_row(result)
    st.metric("Field accuracy", percent(summary["extract_acc"]))
    st.caption(f"{summary['charts']} charts · {summary['provider']} · {result['split']} split")
    for label in ["extraction", "redaction", "reconciliation", "retrieval", "answers"]:
        with st.expander(label.capitalize()):
            st.json(result[label])


st.set_page_config(page_title="Air-medical operations", layout="wide")
st.sidebar.title("Air-medical operations")
page = st.sidebar.radio("Workspace", [*DEPARTMENTS, "Evaluations", "Model routing"])
st.sidebar.caption("Local prototype · Synthetic data")
st.title(page)
if page in DEPARTMENTS:
    department(page)
elif page == "Evaluations":
    evaluations()
else:
    st.write("Recorded calls grouped by task and provider. Success rate measures calls that returned successfully.")
    st.dataframe(summarize(log.read()), hide_index=True, width="stretch")
