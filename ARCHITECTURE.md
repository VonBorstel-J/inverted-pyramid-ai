# Inverted Pyramid AI: architecture and build contract

This system gives governed AI answers over the operations data of an air-medical transport company: billing and revenue cycle, flight operations, finance, and HR. **All data is synthetic.** No real patient, employee, aircraft or company data is ever used. The system has no link to any real company.

This document records the original design and its test targets. The [claim walkthrough](docs/claim-review.md) and [local checks](docs/LOCAL-VERIFICATION.md) report the results measured with synthetic records.

## 1. The idea

Frontline work produces the most data: flight charts (ePCRs), claims, dispatch logs, maintenance tickets, ledgers, shifts. Each department is a funnel turned upside down. Raw volume is at the wide top. Inference waves narrow it to high-signal facts, which flow into one governed semantic pool:

```
   BILLING / RCM         FLIGHT OPS            FINANCE              HR / CREW
 ePCRs, claims,       dispatch, tails,      base ledger,         roster, licenses,
 payer policies       maintenance, SOPs     leases, payments     shifts, handbook
  \  WAVE 1:   /       \  WAVE 1:   /        \  WAVE 1:   /        \  WAVE 1:   /
   \ extract  /         \ extract  /          \ normalize/          \ extract  /
    \ redact /           \ index  /            \ & tag  /            \ & track/
     \WAVE 2/             \WAVE 2/              \WAVE 2/              \WAVE 2/
  reconcile vs        SOP + AOG impact       variance vs plan     compliance and
  payer rules                                                     overtime
        \/                   \/                    \/                    \/
 ==================== GOLD: UNIFIED GOVERNED SEMANTIC POOL ====================
  entity graph  transport_id <-> tail_number <-> base_id <-> claim_id <-> crew_id
  semantic views (governed metrics) + hybrid search with citations
  role-based views: masking and row access (role-specific views for synthetic records)
```

**Example cross-department question:** "What did tail N412AM's hydraulic AOG in September cost us?" The answer should combine:
- the missed transports and the revenue lost to them (Ops → Finance);
- crew overtime (HR);
- the claims at risk of denial in the same window (Billing).

Every number in the answer carries a citation.

## 2. Stack (runs on a phone or a laptop, $0)

- **Language:** Python 3.11+, with a virtual environment in `.venv/`.
- **Warehouse:** DuckDB, in the file `warehouse.duckdb`, which git ignores. It has three schemas: `bronze` (raw), `silver` (cleaned, de-identified, typed) and `gold` (the pool).
- **Schema gates:** pydantic models. Every record that crosses a wave validates, or is quarantined into the `*_rejects` table with the reason.
- **Search:**
  - BM25 (`rank-bm25`);
  - vectors from a local embedding model (EmbeddingGemma 300M through `llama-server --embedding`), stored in DuckDB;
  - the two merged with reciprocal rank fusion (RRF).
  - With no embedding server running, it falls back to BM25 only, and says so.
- **LLM router** (`llm/`). Each task names a provider; every call is logged (section 6). The providers:
  - `rules`: deterministic Python and regex;
  - `local`: llama.cpp's OpenAI-compatible server (Qwen3-4B-Instruct by default);
  - `claude`: `claude -p` headless;
  - `codex`: `codex exec`;
  - `gemini`: `gemini -p`.
- **App:** Streamlit. There is one page per department, plus a cross-department page, an evals page and a routing page.
- **Later, the Snowflake port:** `docs/SNOWFLAKE.md` maps each piece to its Snowflake equivalent:
  - bronze, silver and gold become schemas;
  - Wave 1 becomes AI_EXTRACT / AI_REDACT;
  - search becomes Cortex Search;
  - gold views become Semantic Views and Cortex Analyst;
  - roles become masking and row access policies;
  - the app becomes Streamlit in Snowflake.

## 3. Shared keys and bronze tables (`data_gen/`)

- **Determinism:** one entry point, `python -m data_gen.generate_all --seed 42 --out warehouse.duckdb`, writes every bronze table. The same seed gives byte-identical data.
- **Window and scale:** dates run from 2026-06-01 to 2026-10-31. There are 8 bases, each with 1 or 2 aircraft (made-up tail numbers in the format N###XX that are not real registrations; for example N412AM is acceptable only as fiction), and about 2,000 transports.
- **Shared keys:**
  - `base_id`, for example `DEN-01`;
  - `tail_number`;
  - `transport_id`, written `TR-2026-000123`;
  - `claim_id`, written `CL-…`;
  - `crew_id`, written `CR-…`;
  - `doc_id`.
  - The same key spelling is used everywhere.
- **Planted signals:** each department plants known signals with ground truth in `bronze.truth_*` tables, which evals use and which waves never read. Examples:
  - claims missing a ground-transport contraindication (25%);
  - 5 to 8 AOG events with lost transports;
  - crew licenses that expire inside the window;
  - ledger variances;
  - superseded SOPs that must never be cited.

**Billing / RCM (the only department with PHI, and all of it fake):**
- `bronze.transports`: transport_id, base_id, tail_number, date, payer, cpt_code (A0431 rotary, A0430 fixed-wing), loaded_miles (billed as A0436), icd10_code, billed_amount, collected_amount, claim_status.
- `bronze.epcrs`: doc_id, transport_id, raw_text. This is free text with fake PHI (name, DOB, SSN, MRN, phone, address) and a clinical narrative. It must vary in wording, so that rules alone cannot solve it.
- `bronze.claims`: claim_id, transport_id, payer, submitted_date, status, denial_code (CO-50 and others), paid_amount.
- `bronze.documents` rows of dept BILLING: payer medical-necessity policies, each with doc_id, title, status, effective_date and content.

**Flight ops:**
- `bronze.aircraft`: tail_number, base_id, type, in_service.
- `bronze.dispatch_log`: request_id, base_id, ts, outcome (accepted / declined_weather / declined_aog / declined_crew), transport_id when flown.
- `bronze.maintenance_events`: mx_id, tail_number, base_id, start, end, aog_hours, issue_text (free text), missed_requests.
- `bronze.weather_declines`.
- `bronze.documents` rows of dept OPS: SOPs, including ACTIVE and SUPERSEDED versions of the same SOP with conflicting numbers.

**Finance:**
- `bronze.base_ledger`: base_id, month, revenue, fuel, maintenance_cost, crew_cost, lease_cost.
- `bronze.budget`: base_id, month, line, planned.
- `bronze.leases`: tail_number, monthly_cost.
- `bronze.documents` rows of dept FINANCE: policy memos.

**HR / crew:**
- `bronze.crew`: crew_id, base_id, role (pilot / flight nurse / flight paramedic / mechanic), hire_date.
- `bronze.licenses`: crew_id, kind, expires.
- `bronze.shifts`: crew_id, date, hours, overtime_hours, reason_text (free text, sometimes naming a tail or an AOG).
- `bronze.documents` rows of dept HR: the handbook, including duty-limit rules.

## 4. Waves (`waves/`)

Every wave step is a function `run(con, ctx) -> StepResult`. It reads one layer and writes the next, through a pydantic model. Every output row carries:
- `prov_source` (the bronze key or keys);
- `prov_wave`;
- `prov_step`;
- `prov_provider` (rules, local, claude …);
- `prov_model`;
- `prov_prompt_version`;
- `prov_at`.

`python -m waves.pipeline [--provider wave1=local|claude|rules]` runs everything in order and is idempotent.

**Wave 1 (per department, the wide top):**
- **Billing:** extract the structured fields from each ePCR:
  - diagnosis;
  - instability evidence;
  - the ground-contraindication statement (verbatim span or null);
  - physician certification yes/no;
  - loaded miles.

  Write redacted chart text into `silver.epcr_deid` and measure the planted identifiers still visible afterward. The redaction is rules first (regex), plus the model for names in free text. The evaluation compares the output with labeled spans supplied by the synthetic data generator.
- **Ops:** parse maintenance free text into issue category, system and severity. Index SOPs, keeping only ACTIVE documents effective by the event date.
- **Finance:** normalize and tag each ledger line against its budget line.
- **HR:** extract each license's expiry status and the overtime cause from reason_text, linking to a tail or AOG when the text names one.

**Wave 2 (per department):**
- **Billing:** reconcile each claim against the payer policy that was in effect on the transport date. The output is `at_risk` with a reason and a citation [doc_id, section, effective_date].
- **Ops:** AOG impact: missed requests turned into estimated lost transports.
- **Finance:** variance against plan, with its drivers.
- **HR:** compliance flags (expired license on a flown shift, duty-limit breach) and overtime that AOG caused.

**Gold pool:**
- `gold.entity_links`, the entity graph;
- `gold.*` views with governed metric definitions, in one place (`gold/metrics.sql`);
- `gold.search_chunks`, the hybrid index over ACTIVE documents and the de-identified narratives.

**Rule: deterministic first.** SQL and Python do math, thresholds and joins. Models only extract, classify and write prose. A model never computes a number that ends up in an answer.

## 5. Governance (`governance/`)

- **Roles:** billing_phi, billing, ops, finance, hr, exec.
- **Enforcement:** `governance/views.sql` builds one schema per role (`role_billing.*` and so on). Raw PHI columns appear only in `role_billing_phi`; every other role sees de-identified or aggregated data. This mirrors Snowflake masking and row access policies.
- **Routing:** the app and the question-answering layer take a role and query only that role's schema.
- **Tests:**
  - no PHI string from bronze appears in any non-PHI role's view;
  - an exec cross-department answer never contains an identifier.
- **Tracing:** no PHI is ever sent to a model except the `local` provider, and the `claude`/`codex`/`gemini` providers get de-identified text only. The router enforces this by refusing an input that contains a planted identifier pattern.

## 6. Routing log (the blueprint for an orchestrator)

Every model call appends one JSON line to `runs/routing.jsonl` with these fields:
- `ts`, `task` (e.g. `wave1.billing.extract`), `provider`, `model`;
- `ms`, `input_chars`, `output_chars`, `tokens_in`/`tokens_out` when known;
- `ok` and `error`;
- `quality`, the score from evals, filled in later by `evals.score`.

`python -m llm.report` summarizes it per task and provider: count, median ms, success rate, quality. The Streamlit routing page shows the same summary. It is how we pick the cheapest provider that meets the quality bar for each task.

Builders log their own work too. `runs/build-log.jsonl` gets one line per build job: who (claude/codex/gemini/local), what, files, minutes, and a review verdict.

## 7. Evals (`evals/`)

- **Extraction:** field accuracy per field, against `bronze.truth_*`.
- **Redaction:** recall per identifier type (target 100%) and precision.
- **Retrieval:** hit@5 for a set of 40+ questions with gold doc_ids, including traps (questions whose only matching SOP is superseded).
- **Answers:** groundedness (every claim cited, every citation real and ACTIVE), plus correctness against gold numbers computed in SQL.
- **Local vs cloud:** the same Wave 1 billing extraction on the same 200 ePCRs with `local` (Qwen3-4B) and `claude`, scored and timed, written to `evals/results/`.

## 8. Rules for every builder

- Synthetic data only. No real company names, logos or people, and no real tail numbers on purpose.
- Stdlib plus the listed dependencies. Pin versions in `requirements.txt`.
- Tests: `python -m pytest -q` if pytest is present, else `python -m unittest`. Every module gets tests with fakes for models (no network in tests).
- Small commits, each with a clear message, authored as VonBorstel-J.
- Write only inside this repository folder.
