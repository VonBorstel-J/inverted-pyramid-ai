"""Run the evals (ARCHITECTURE.md section 7) for one split and one provider.

    python -m evals.run --split default|hard --provider wave1.billing=rules|local|claude [--limit N]
                        [--provider answer=PROVIDER] [--answers N] [--start-servers] [--db PATH]

Scores:
- extraction: field accuracy per field vs bronze.truth_epcrs (the Wave 1
  billing extraction re-run through the router on N sampled charts);
- redaction: recall per identifier type and precision vs bronze.truth_phi;
- reconciliation: at_risk accuracy, cited policy accuracy, and policy version
  accuracy when the split has truth for it;
- retrieval: hit@5 / recall@5 on evals/questions.yaml plus questions derived
  from the split's truth tables (superseded traps, payer policy versions);
- answers: groundedness (every claim cited, every citation real and ACTIVE)
  and numeric correctness vs numbers computed in SQL from truth tables.

Results go to evals/results/<timestamp>-<split>-<provider>.json; the score of
every model call is written back into runs/routing.jsonl `quality`.
--start-servers starts llama-server for embeddings (to warm the cache) and,
for provider local, the chat model, one at a time, and stops each after.
"""
import argparse
import calendar
import datetime as dt
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

import llm
from answer import check as C
from answer import plan as PL
from evals import metrics as M
from evals import score
from llm.phi import PHI_SAFE_PROVIDERS, find_phi
from waves import billing as WB
from waves import prompts, redact as R
from waves.framework import columns, fetch, has_table
from waves.schemas import EpcrExtract, NameList

REPO = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
DBS = {"default": REPO / "warehouse.duckdb", "hard": REPO / "warehouse_hard.duckdb"}
EMBED_PORT, CHAT_PORT = 8092, 8091
MIN_FREE_MB = 2500
TOPIC = {"missing ground contraindication": "documenting why ground transport was clinically unsafe",
         "missing certification": "a signed physician certification",
         "missing instability evidence": "evidence of clinical instability",
         "loaded miles over policy cap": "the maximum loaded miles per leg"}


# ------------------------------------------------------------ setup
def parse_providers(items):
    out = {"wave1.billing": "rules"}
    for it in items or []:
        k, v = it.split("=", 1) if "=" in it else ("wave1.billing", it)
        if v not in llm.router.KNOWN:
            raise SystemExit(f"unknown provider {v!r}")
        out[k.strip()] = v.strip()
    out.setdefault("answer", out["wave1.billing"])
    return out


def prepare(split, db):
    """Make sure the split's warehouse exists and has been through the rules
    pipeline (silver, gold, role schemas)."""
    import duckdb
    db = Path(db)
    if not db.exists():
        cmd = [sys.executable, "-m", "data_gen.generate_all", "--seed", "42", "--out", str(db)]
        if split != "default":
            cmd += ["--split", split]
        subprocess.run(cmd, check=True, cwd=REPO)
    con = duckdb.connect(str(db))
    try:
        ready = all(has_table(con, t) for t in ("silver.epcr_deid", "gold.search_chunks", "role_exec.aog_events"))
        if not ready:
            from waves import pipeline
            pipeline.run(con)
    finally:
        con.close()


def free_mb():
    try:
        out = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=10).stdout
        head, mem = out.splitlines()[0].split(), out.splitlines()[1].split()
        return int(mem[1 + head.index("available")])
    except Exception:
        return 0


class Server:
    """Context manager: start one llama-server (if memory allows), stop it after."""
    def __init__(self, model, port):
        self.model, self.port, self.handle = model, port, None

    def __enter__(self):
        from llm import local_server
        mb = free_mb()
        if mb < MIN_FREE_MB:
            raise RuntimeError(f"only {mb} MB available; need > {MIN_FREE_MB} MB to start llama-server")
        # Two model calls per chart put a 200-chart phone run beyond the
        # ordinary two-hour server lease. __exit__ still stops it on completion.
        lease = "12h" if self.model == "chat" else "2h"
        self.handle = local_server.start(self.model, self.port, lease=lease)
        return self.handle

    def __exit__(self, *a):
        from llm import local_server
        local_server.stop(self.port)
        return False


def load_questions(path=HERE / "questions.yaml"):
    import yaml
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def for_split(items, split):
    return [q for q in items if not q.get("splits") or split in q["splits"]]


def derived_retrieval(con):
    """Questions built from the split's truth tables (no document text read)."""
    out = []
    if has_table(con, "bronze.truth_sop_traps"):
        for i, t in enumerate(fetch(con, "SELECT * FROM bronze.truth_sop_traps ORDER BY active_doc_id")):
            out.append({"id": f"truth-sop-{i}", "q": t["question"], "role": "ops",
                        "gold": [[t["active_doc_id"], t["section"]]], "forbid": [t["superseded_doc_id"]],
                        "tags": ["derived", "sop"]})
            if t.get("retired_question"):
                gold = [[t["active_doc_id"], t["section"]]] if t.get("retired_question_has_active_answer") else []
                out.append({"id": f"truth-trap-{i}", "q": t["retired_question"], "role": "ops", "gold": gold,
                            "forbid": [t["superseded_doc_id"]], "tags": ["derived", "trap"]})
    if has_table(con, "bronze.truth_claim_risk") and has_table(con, "bronze.claims"):
        rows = fetch(con, """SELECT coalesce(c.payer, t.payer) AS payer, r.doc_id, r.section, r.reason,
                                    min(CAST(t.date AS DATE)) AS first_date
                             FROM bronze.truth_claim_risk r JOIN bronze.claims c USING (claim_id)
                             JOIN bronze.transports t USING (transport_id)
                             WHERE r.at_risk AND r.doc_id IS NOT NULL
                             GROUP BY ALL ORDER BY payer, r.doc_id, r.section, r.reason""")
        for i, r in enumerate(rows):
            topic = TOPIC.get(r["reason"], r["reason"].replace("missing ", ""))
            out.append({"id": f"truth-pol-{i}", "q": f"What does the {r['payer']} transport policy require about {topic}?",
                        "role": "billing", "as_of": str(r["first_date"]), "gold": [[r["doc_id"], r["section"]]],
                        "tags": ["derived", "policy"]})
    return out


def derived_answers(con):
    if not has_table(con, "bronze.truth_aog_impacts"):
        return []
    ot = "bronze.truth_overtime" if has_table(con, "bronze.truth_overtime") else None
    rows = fetch(con, f"""WITH i AS (SELECT tail_number, month, mx_id, sum(lost_transports) AS lt, sum(lost_revenue) AS lr
                                     FROM bronze.truth_aog_impacts GROUP BY ALL)
                          SELECT i.*, {'(SELECT sum(overtime_cost) FROM ' + ot + ' o WHERE o.mx_id = i.mx_id)' if ot else 'NULL'} AS oc
                          FROM i ORDER BY month, tail_number""")
    out = []
    for r in rows:
        m = r["month"]
        out.append({"id": f"truth-aog-{r['mx_id']}", "role": "exec", "kind": "cross_department",
                    "q": f"What did tail {r['tail_number']}'s AOG in {calendar.month_name[m.month]} cost us?",
                    "expect_numbers": [x for x in (r["lt"], r["lr"], r["oc"]) if x is not None],
                    "tags": ["derived"]})
    return out


def all_questions(con, split, qs=None):
    qs = qs or load_questions()
    ret = for_split(qs.get("retrieval", []), split) + derived_retrieval(con)
    ans = for_split(qs.get("answers", []), split)
    seen = {a["q"] for a in ans}
    ans += [a for a in derived_answers(con) if a["q"] not in seen]
    return ret, ans


# ------------------------------------------------------------ extraction + redaction + reconciliation
def sample_charts(con, limit):
    sql = ("SELECT e.doc_id, e.transport_id, e.raw_text FROM bronze.epcrs e "
           "JOIN bronze.truth_epcrs t USING (doc_id) ORDER BY md5(e.doc_id)")
    return fetch(con, sql + (f" LIMIT {int(limit)}" if limit else ""))


def extract_one(raw, provider, model, opts):
    """Wave 1 billing for one chart through the router, exactly as the pipeline
    does it. Returns (deid, pred_or_None, note, spans, ms)."""
    names_prov = provider if provider in PHI_SAFE_PROVIDERS else "rules"
    v_names, t_names = prompts.BILLING_NAMES
    v_ex, t_ex = prompts.BILLING_EXTRACT
    ms = {}
    with score.Span() as s_names:
        t0 = time.perf_counter()
        try:
            got = llm.call(WB.NAMES_TASK, raw if names_prov == "rules" else t_names.format(text=raw),
                           provider=names_prov, schema=NameList, model=model if names_prov == provider else None,
                           rules=lambda _p: {"names": R.rule_names(raw)}, **opts)
            names = [n for n in got.names if n and n in raw]
        except Exception:
            names = []
        ms["names"] = (time.perf_counter() - t0) * 1000
    deid, _ = R.redact(raw, names)
    spans = {"names": s_names.lines}
    if find_phi(deid):
        return deid, None, "identifier pattern left after redaction", spans, ms
    with score.Span() as s_ex:
        t0 = time.perf_counter()
        try:
            got = llm.call(WB.EXTRACT_TASK, deid if provider == "rules" else t_ex.format(text=deid),
                           provider=provider, schema=EpcrExtract, model=model,
                           rules=lambda _p: WB.rule_extract(deid), **opts)
            pred, note = got.model_dump(), None
        except Exception as e:
            pred, note = None, f"router: {type(e).__name__}: {str(e)[:160]}"
        ms["extract"] = (time.perf_counter() - t0) * 1000
    spans["extract"] = s_ex.lines
    if pred and pred.get("ground_contraindication") and pred["ground_contraindication"].strip() not in deid:
        pred, note = None, "ground_contraindication is not a verbatim span of the chart"
    return deid, pred, note, spans, ms


def truth_versions(con):
    """{claim_id: {"doc_id", "effective_date", "version"}} when the split
    carries policy-version truth, else None."""
    for table in ("bronze.truth_policy_version", "bronze.truth_policy_versions", "bronze.truth_claim_risk"):
        if not has_table(con, table):
            continue
        cols = columns(con, table)
        doc = next((c for c in ("policy_doc_id", "truth_policy_doc_id") if c in cols), None)
        eff = next((c for c in ("policy_effective_date", "effective_date", "truth_policy_effective_date") if c in cols), None)
        ver = next((c for c in ("truth_policy_version", "policy_version", "version") if c in cols), None)
        if table.endswith("truth_claim_risk") and not (eff or ver):
            continue
        if table != "bronze.truth_claim_risk" and not doc and "doc_id" in cols:
            doc = "doc_id"
        if "claim_id" not in cols or not (doc or eff or ver):
            continue
        sel = ", ".join(f"{c} AS {a}" for c, a in ((doc, "doc_id"), (eff, "effective_date"), (ver, "version")) if c)
        return {r["claim_id"]: r for r in fetch(con, f"SELECT claim_id, {sel} FROM {table}")}
    return None


def reconcile(con, preds):
    """Claims of the sampled transports against the policy in effect on the
    transport date, with the predicted extracts (waves/billing.py logic)."""
    if not preds:
        return []
    doc_cols = [c for c in ("sections", "payer") if c in columns(con, "bronze.documents")]
    policies = fetch(con, f"""SELECT doc_id, title, status, CAST(effective_date AS DATE) AS effective_date, content
                              {''.join(', ' + c for c in doc_cols)} FROM bronze.documents WHERE upper(dept) = 'BILLING'""")
    claims = fetch(con, """SELECT c.claim_id, c.transport_id, coalesce(c.payer, t.payer) AS payer,
                                  CAST(t.date AS DATE) AS transport_date
                           FROM bronze.claims c JOIN bronze.transports t USING (transport_id)
                           WHERE c.transport_id IN (SELECT unnest(?)) ORDER BY c.claim_id""", [list(preds)])
    out = []
    for c in claims:
        pol = WB.policy_in_effect(policies, c["payer"], c["transport_date"])
        ex = preds.get(c["transport_id"])
        if pol is None:
            out.append({"claim_id": c["claim_id"], "at_risk": True, "policy_doc_id": None})
            continue
        if ex is None:
            failed = [("epcr", WB.policy_sections(pol)[0][0])]
        else:
            failed = WB.check_claim(ex, pol)
        sec = failed[0][1] if failed else None
        out.append({"claim_id": c["claim_id"], "at_risk": bool(failed), "policy_doc_id": pol["doc_id"],
                    "policy_section": sec, "policy_effective_date": pol["effective_date"]})
    return out


def eval_charts(con, provider, limit, model=None, opts=None):
    opts = opts or {}
    charts = sample_charts(con, limit)
    truth = {r["doc_id"]: r for r in fetch(con, "SELECT * FROM bronze.truth_epcrs WHERE doc_id IN (SELECT unnest(?))",
                                           [[c["doc_id"] for c in charts]])}
    phi = {}
    for r in fetch(con, "SELECT * FROM bronze.truth_phi WHERE doc_id IN (SELECT unnest(?))", [[c["doc_id"] for c in charts]]):
        phi.setdefault(r["doc_id"], []).append(r)
    per, scored, red, assign, preds, ms_ex, ms_names = [], [], [], [], {}, [], []
    for c in charts:
        deid, pred, note, spans, ms = extract_one(c["raw_text"], provider, model, opts)
        s = M.score_extract(pred, truth[c["doc_id"]])
        r = M.score_redaction(c["raw_text"], deid, phi.get(c["doc_id"], []))
        scored.append(s)
        red.append(r)
        preds[c["transport_id"]] = pred
        acc = sum(s.values()) / len(s)
        found = sum(f for f, _ in r["by_type"].values())
        total = sum(n for _, n in r["by_type"].values())
        name_rec = (r["by_type"].get("name", [0, 0])[0] / r["by_type"]["name"][1]) if r["by_type"].get("name") else None
        assign += [(i, WB.EXTRACT_TASK, acc) for i in spans.get("extract", [])]
        assign += [(i, WB.NAMES_TASK, name_rec if name_rec is not None else (found / total if total else None))
                   for i in spans.get("names", [])]
        ms_ex.append(ms.get("extract"))
        ms_names.append(ms.get("names"))
        per.append({"doc_id": c["doc_id"], "fields": s, "accuracy": round(acc, 4), "note": note,
                    "redaction": r["by_type"], "ms": {k: round(v, 1) for k, v in ms.items()}})
    rec = reconcile(con, preds)
    truth_risk = {r["claim_id"]: r for r in fetch(con, "SELECT * FROM bronze.truth_claim_risk")}
    versions = truth_versions(con)
    rs = [M.score_reconcile(p, truth_risk[p["claim_id"]], (versions or {}).get(p["claim_id"]))
          for p in rec if p["claim_id"] in truth_risk]
    return {
        "extraction": {**M.field_accuracy(scored), "charts": len(charts),
                       "rejected": sum(1 for p in per if p["note"]), "median_ms": M.median(ms_ex)},
        "redaction": {**M.redaction_summary(red), "median_ms_names": M.median(ms_names),
                      "names_provider": provider if provider in PHI_SAFE_PROVIDERS else "rules"},
        "reconciliation": {"claims": len(rs), "at_risk_accuracy": M.rate(rs, "at_risk"),
                           "policy_accuracy": M.rate(rs, "policy"),
                           "policy_version_accuracy": M.rate(rs, "policy_version"),
                           "has_version_truth": versions is not None},
    }, per, assign


# ------------------------------------------------------------ retrieval
def eval_retrieval(con, questions, embedder):
    from search import search
    per, modes = [], {}
    for q in questions:
        kinds = None if q.get("narratives") else {"doc"}
        res = search(con, q["q"], role=q.get("role", "exec"), as_of=q.get("as_of"), k=5, embedder=embedder, kinds=kinds)
        cites = res.citations
        hit, recall, bad = M.retrieval_hit(cites, q.get("gold") or [], q.get("forbid") or [], q.get("expect_none", False))
        modes[res.mode] = modes.get(res.mode, 0) + 1
        per.append({"id": q["id"], "q": q["q"], "hit": hit, "recall": recall, "forbidden_seen": bad,
                    "mode": res.mode, "top5": cites, "tags": q.get("tags", [])})
    superseded = {r[0] for r in con.execute("SELECT doc_id FROM bronze.documents WHERE status <> 'ACTIVE'").fetchall()}
    sup_seen = sum(1 for p in per if any(c[0] in superseded for c in p["top5"]))
    traps = [p for p in per if "trap" in p["tags"]]
    return {"questions": len(per), "hit@5": M.rate(per, "hit"), "recall@5": M.rate(per, "recall"),
            "trap_hit@5": M.rate(traps, "hit"), "superseded_returned": sup_seen, "modes": modes}, per


# ------------------------------------------------------------ answers
def verify_citations(con, a):
    """Every cited source is real: a gold view, or an ACTIVE document section
    in effect on as_of, or a de-identified narrative."""
    views = {f"gold.{r[0]}" for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'gold'").fetchall()}
    bad = []
    for s in a.sources:
        if s["n"] not in a.cited:
            continue
        c = s["citation"]
        if s["kind"] == "sql":
            ok = c[0] in views
        elif c[1] == "narrative":
            ok = bool(con.execute("SELECT count(*) FROM silver.epcr_deid WHERE doc_id = ?", [c[0]]).fetchone()[0])
        else:
            ok = bool(con.execute(
                """SELECT count(*) FROM bronze.documents d JOIN silver.doc_sections s USING (doc_id)
                   WHERE d.doc_id = ? AND s.section = ? AND d.status = 'ACTIVE'
                     AND CAST(d.effective_date AS DATE) <= CAST(? AS DATE)""", [c[0], c[1], str(a.as_of)]).fetchone()[0])
        if not ok:
            bad.append(c)
    return not bad, bad


def expected_numbers(con, q):
    if q.get("expect_numbers") is not None:
        return list(q["expect_numbers"])
    if q.get("expect_numbers_sql"):
        row = con.execute(q["expect_numbers_sql"]).fetchone()
        return [float(v) for v in (row or []) if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return []


def eval_answers(con, questions, provider, embedder, model=None, opts=None):
    from answer import ask
    opts = opts or {}
    per, assign = [], []
    for q in questions:
        with score.Span() as sp:
            a = ask(q["q"], q.get("role", "exec"), as_of=q.get("as_of"), provider=provider, con=con,
                    embedder=embedder, model=model, **opts)
        exp = expected_numbers(con, q)
        nums = C.numbers(a.answer)
        refusal_ok = a.refused == bool(q.get("expect_refusal"))
        grounded, bad = (None, [])
        if not a.refused:
            real, bad = verify_citations(con, a)
            grounded = real and bool(a.checks.get("post_check", {}).get("ok"))
        numeric = None if q.get("expect_refusal") else (0.0 if exp and a.refused else M.numeric_correctness(nums, exp))
        cited_docs = {c[0] for c in a.citations}
        docs_ok = set(q.get("expect_docs") or []) <= cited_docs
        forbid_ok = not (set(q.get("forbid_docs") or []) & cited_docs) and \
            not any(t.lower() in a.answer.lower() for t in q.get("forbid_text") or [])
        parts = [refusal_ok, docs_ok, forbid_ok] + [x for x in (grounded, numeric) if x is not None]
        quality = sum(float(x) for x in parts) / len(parts)
        kind_ok = None if not q.get("kind") else a.kind == q["kind"]
        for i in sp.lines:
            assign.append((i, "answer.write", quality))
            assign.append((i, "answer.classify", None if kind_ok is None else float(kind_ok)))
        per.append({"id": q["id"], "q": q["q"], "role": q.get("role", "exec"), "kind": a.kind, "kind_ok": kind_ok,
                    "refused": a.refused, "refusal_ok": refusal_ok, "grounded": grounded, "bad_citations": bad,
                    "numeric": numeric, "expected_numbers": exp, "docs_ok": docs_ok, "forbid_ok": forbid_ok,
                    "quality": round(quality, 4), "answer": a.answer, "citations": a.citations,
                    "reason": a.reason, "search_mode": a.search_mode, "attempts": a.attempts, "ms": round(a.ms, 1)})
    answered = [p for p in per if not p["refused"]]
    return {"questions": len(per), "answered": len(answered), "refusal_accuracy": M.rate(per, "refusal_ok"),
            "groundedness": M.rate([p for p in answered], "grounded"),
            "numeric_correctness": M.rate([p for p in per if p["numeric"] is not None], "numeric"),
            "kind_accuracy": M.rate([p for p in per if p["kind_ok"] is not None], "kind_ok"),
            "quality": M.rate(per, "quality"), "median_ms": M.median([p["ms"] for p in per])}, per, assign


# ------------------------------------------------------------ orchestration
def warm_embeddings(con, retrieval, answers):
    """With an embedding server up: embed every document chunk and every query
    the eval will issue, so the rest of the run needs no server."""
    from search import index
    from search.embed import Embedder
    e = Embedder()
    if not e.available():
        return {"warmed": False, "note": f"no embedding server at {e.url}"}
    docs = [index.embed_text(r) for r in index.load(con) if r["kind"] == "doc"]
    e.embed_docs(docs)
    queries = [q["q"] for q in retrieval]
    for a in answers:
        queries += PL.search_queries(a["q"], "cross_department")
    for q in dict.fromkeys(queries):
        e.embed_query(q)
    return {"warmed": True, "docs": len(docs), "queries": len(set(queries)), **e.stats}


def run(split="default", providers=None, limit=5, db=None, answers_limit=None, start_servers=False,
        write_quality=True, out_dir=HERE / "results", echo=print, opts=None, questions=None):
    import duckdb
    from search.embed import Embedder
    providers = providers or parse_providers([])
    prov, aprov = providers["wave1.billing"], providers["answer"]
    db = Path(db or DBS[split])
    t0 = time.perf_counter()
    prepare(split, db)
    con = duckdb.connect(str(db), read_only=True)
    servers = {}
    try:
        retrieval_qs, answer_qs = all_questions(con, split, questions)
        if answers_limit is not None:
            answer_qs = answer_qs[:answers_limit]
        elif limit:
            answer_qs = answer_qs[:limit]
        if start_servers:
            with Server("embed", EMBED_PORT) as h:
                servers["embed"] = {"ready_s": h.get("ready_s"), "ledger_id": h.get("ledger_id")}
                servers["embed"].update(warm_embeddings(con, retrieval_qs, answer_qs))
        embedder = Embedder()
        res = {"split": split, "db": str(db), "providers": providers, "limit": limit,
               "started": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        echo(f"evals: split={split} providers={providers} limit={limit} answers={len(answer_qs)}")
        res["retrieval"], per_ret = eval_retrieval(con, retrieval_qs, embedder)
        need_chat = start_servers and "local" in (prov, aprov)
        chat = Server("chat", CHAT_PORT) if need_chat else None
        if chat:
            h = chat.__enter__()
            servers["chat"] = {"ready_s": h.get("ready_s"), "ledger_id": h.get("ledger_id")}
        try:
            charts, per_chart, assign = eval_charts(con, prov, limit, opts=opts)
            res.update(charts)
            res["answers"], per_ans, assign2 = eval_answers(con, answer_qs, aprov, embedder, opts=opts)
        finally:
            if chat:
                chat.__exit__(None, None, None)
        res["servers"] = servers
        res["quality_lines"] = score.apply(assign + assign2) if write_quality else 0
        res["ms_total"] = round((time.perf_counter() - t0) * 1000)
        res["per_item"] = {"charts": per_chart, "retrieval": per_ret, "answers": per_ans}
    finally:
        con.close()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{stamp}-{split}-{prov}.json"
    path.write_text(json.dumps(res, indent=1, default=str))
    res["path"] = str(path)
    echo(summary_table([res]))
    echo(f"results: {path}")
    return res


def summary_row(r):
    e, d, c, t, a = r["extraction"], r["redaction"], r["reconciliation"], r["retrieval"], r["answers"]
    return {"provider": r["providers"]["wave1.billing"], "charts": e.get("charts"), "extract_acc": e.get("overall"),
            "contraind": e.get("ground_contraindication"), "redact_recall": d.get("recall"),
            "redact_prec": d.get("precision"), "reconcile": c.get("at_risk_accuracy"), "policy": c.get("policy_accuracy"),
            "version": c.get("policy_version_accuracy"), "hit@5": t.get("hit@5"), "retr_mode": ",".join(t.get("modes", {})),
            "answers": a.get("questions"), "grounded": a.get("groundedness"), "numeric": a.get("numeric_correctness"),
            "extract_ms": e.get("median_ms"), "answer_ms": a.get("median_ms")}


def summary_table(results):
    rows = [summary_row(r) for r in results]
    cols = list(rows[0]) if rows else []
    w = {c: max(len(c), *(len("" if r[c] is None else str(r[c])) for r in rows)) for c in cols}
    lines = [" ".join(c.ljust(w[c]) for c in cols), " ".join("-" * w[c] for c in cols)]
    for r in rows:
        lines.append(" ".join(("" if r[c] is None else str(r[c])).ljust(w[c]) for c in cols))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", default="default", choices=sorted(DBS))
    ap.add_argument("--provider", action="append", default=[], metavar="TASK=PROVIDER",
                    help="wave1.billing=rules|local|claude (and optionally answer=...)")
    ap.add_argument("--limit", type=int, default=None, help="charts to extract (default: all with truth)")
    ap.add_argument("--answers", type=int, default=None, help="answer questions to run (default: min(limit, all))")
    ap.add_argument("--db", default=None)
    ap.add_argument("--start-servers", action="store_true",
                    help="start/stop llama-server for embeddings and (provider local) chat, one at a time")
    ap.add_argument("--no-quality", action="store_true", help="do not write scores into runs/routing.jsonl")
    a = ap.parse_args(argv)
    run(a.split, parse_providers(a.provider), a.limit, a.db, a.answers, a.start_servers, not a.no_quality)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
