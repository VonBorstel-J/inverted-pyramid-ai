# Inverted Pyramid AI

Why did a September flight land in the billing review queue? For claim `CL-2026-000598`, the chart gives no reason ground transport would have been unsafe. The console shows the flag alongside the payer’s documentation requirement and the date it took effect. A billing reviewer can check the original finding and decide whether the chart needs a second look. [Follow the claim through the warehouse](docs/claim-review.md).

I built this local DuckDB application with synthetic flight charts, claims, maintenance records, and crew schedules. It also lets an operations analyst trace an aircraft outage to missed flights and overtime. SQL views calculate the amounts. The console lets the reader inspect the records behind them.

## What the model changed

I compared a rules baseline with Qwen3-4B on the same 200 held-out synthetic charts. The test changes the wording of the charts and introduces revisions to payer policies.

| Measure | Rules | Qwen3-4B |
| --- | ---: | ---: |
| Correctly extracted fields | 26.8% | 67.3% |
| Correct claim-risk decisions | 38% | 40% |
| Planted identifiers still visible | 2,272 of 2,672 | 1,367 of 2,672 |

The biggest gain was reading individual fields. On the same sample, Qwen classified only two more claims correctly per hundred. Both runs missed the privacy target of zero visible identifiers. I later expanded the rules for the synthetic chart formats and checked the published billing view against the generator’s labeled spans. [Matched run and scores](evals/results/20261124T060308Z-hard-compare.json) · [Follow-up checks](docs/LOCAL-VERIFICATION.md).

## Try the billing review

The rules demo runs locally with Python 3.13 and needs no model downloads or service credentials. From the repository root on Linux or macOS:

```sh
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m data_gen.generate_all --seed 42 --out warehouse.duckdb
python -m waves.pipeline
python -m streamlit run app/main.py --server.address 127.0.0.1
```

Open the local address Streamlit prints, choose **Billing**, and expand **Inspect a claim and its policy**. Look up the prefilled claim. The result includes policy POL-1, section 4.2, effective June 1, 2026. The department selector gives one person different views of the synthetic warehouse.

To check the scored fields without running a model, open **Evaluations** in the sidebar. To run the automated checks, use `python -m unittest discover -q`.

## Implementation

- [Billing pipeline](waves/billing.py) extracts documentation and reconciles claims against dated policies.
- [Metric definitions](gold/metrics.sql) and [department views](governance/views.sql) show how the warehouse assembles a number before the console presents it.
- [Evaluation harness](evals/run.py) scores extracted fields, identifier removal, retrieval, and answers. The saved phone run documents the model and hardware conditions in the [claim walkthrough](docs/claim-review.md).
- [Snowflake design notes](docs/SNOWFLAKE.md) outline a possible port. All published measurements come from the local DuckDB implementation.
