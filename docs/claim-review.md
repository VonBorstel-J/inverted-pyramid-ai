# From one flight chart to a billing decision

Use the synthetic seed-42 default warehouse for the walkthrough. Use the separate hard warehouse to measure what happens when chart wording and policies change. The same rules that make a good demo can fail on unfamiliar wording.

## Follow the claim

On September 3, 2026, synthetic claim CL-2026-000598 came through DEN-01. Ask what made it count toward the base’s September billing risk. From the repository root, run this query and the follow-up question in the same session.

```bash
.venv/bin/python - <<'PY'
import duckdb
from answer import ask

with duckdb.connect("warehouse.duckdb", read_only=True) as db:
    row = db.execute("""
        SELECT r.claim_id, r.transport_date, r.payer, e.ground_contraindication,
               r.at_risk, r.reason, p.doc_id, p.section, p.effective_date, p.text
        FROM role_billing.claim_reconciliation r
        JOIN role_billing.epcr_extract e USING (transport_id)
        JOIN role_billing.payer_policies p
          ON p.doc_id = r.policy_doc_id AND p.section = r.policy_section
         AND p.effective_date = r.policy_effective_date
        WHERE r.claim_id = 'CL-2026-000598'
    """).fetchone()
    print(row)
    print(ask("How many claims were at risk at DEN-01 in September?",
              role="billing", provider="rules", con=db).render())
PY
```

The extraction found no statement explaining why ground transport was unsafe. The claim was flagged under Fenmere Benefit Trust policy POL-1, section 4.2, effective June 1. That section requires the chart to document why the available ground transport was clinically unsafe. A reviewer can use the citation to check the requirement and revisit the chart. The flag alone cannot establish whether the flight was medically necessary or whether the payer will deny the claim.

The billing answer cites the governed September row. It reports 10 flagged claims among 44, with $135,752 billed on the flagged claims. That dollar figure measures exposure. Actual lost revenue would require payment or denial evidence.

## Interpret the harder test

The hard split changes phrasing and includes policy revisions. The harness samples the same 200 charts for each provider, scores five extracted fields against planted truth, and measures identifier removal separately. It also checks 14 retrieval questions and 8 answer questions. Answer generation reads the previously built rules warehouse, so its numeric score cannot demonstrate the downstream effect of rebuilding the warehouse with Qwen.

## Measured results

| Measure | Rules | Qwen3-4B |
| --- | ---: | ---: |
| Extraction accuracy | 26.8% | 67.3% |
| Identifier recall | 15.0% | 48.8% |
| Planted identifiers left in text | 2,272 | 1,367 |
| Claim risk decisions correct | 38.0% | 40.0% |
| Applicable policy version correct | 70.0% | 70.0% |
| Retrieval hit in first five | 85.7% | 85.7% |
| Answers returned out of eight | 7 | 7 |
| Expected numbers present | 66.7% | 33.3% |
| Median extraction time (ms) | 1 | 45,442 |

Both providers saw 2,672 planted identifiers. The acceptance target is zero left in text. These figures describe synthetic data only. Claude access was unavailable for this comparison.

The extraction timer excludes the separate name-finding call. Groundedness checks citations and source support on returned answers. The numeric score measures how many expected numbers appear.

Saved evidence: [20261123T193325Z-hard-rules](../evals/results/20261123T193325Z-hard-rules.json) · [20261124T060308Z-hard-local](../evals/results/20261124T060308Z-hard-local.json) · [20261124T060308Z-hard-compare](../evals/results/20261124T060308Z-hard-compare.json)

### Phone run conditions

The phone saved each completed chart so the run could resume. The first 12 local charts used the server defaults. The remaining 188 used one inference slot and a 64 MiB prompt-cache limit with the same model and 4,096-token context. After chart 112, the same CPU server ran directly under Android with eight threads for prompt processing and four for generation. The local median combines timings from these configurations. The local result’s total runtime covers the final resumed process; per-chart timings include the earlier saved charts.
