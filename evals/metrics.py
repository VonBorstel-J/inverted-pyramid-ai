"""Eval metric math (ARCHITECTURE.md section 7). Pure functions over small
Python structures, so every formula is unit-tested on tiny known inputs."""
import difflib
import re
import statistics

EXTRACT_FIELDS = ("diagnosis", "instability_evidence", "ground_contraindication", "physician_certification",
                  "loaded_miles")
TAG = re.compile(r"\[[A-Z_]+\]")


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s%/]", " ", str(s or "").lower())).strip()


def text_match(pred, truth, extra_words=4):
    """Equal after normalising, or the truth inside the prediction with at
    most `extra_words` more words."""
    p, t = norm(pred), norm(truth)
    if not t:
        return not p
    if p == t:
        return True
    return t in p and len(p.split()) - len(t.split()) <= extra_words


def span_match(pred, truth):
    """The contraindication sentence: truth inside the prediction or the
    prediction covering at least 60% of the truth."""
    p, t = norm(pred), norm(truth)
    if not p or not t:
        return False
    return t in p or (p in t and len(p) >= 0.6 * len(t))


def score_extract(pred, truth):
    """{field: bool} for one chart. pred None (rejected / failed) scores all
    fields wrong. truth: a bronze.truth_epcrs row."""
    if pred is None:
        return {f: False for f in EXTRACT_FIELDS}
    out = {
        "diagnosis": text_match(pred.get("diagnosis"), truth.get("diagnosis")),
        "instability_evidence": text_match(pred.get("instability_evidence"), truth.get("instability_evidence")),
        "physician_certification": bool(pred.get("physician_certification")) == bool(truth.get("physician_certification")),
        "loaded_miles": (pred.get("loaded_miles") is not None and truth.get("loaded_miles") is not None
                         and int(pred["loaded_miles"]) == int(truth["loaded_miles"]))
        or (pred.get("loaded_miles") is None and truth.get("loaded_miles") is None),
    }
    if truth.get("has_contraindication"):
        out["ground_contraindication"] = span_match(pred.get("ground_contraindication"), truth.get("contraindication"))
    else:
        out["ground_contraindication"] = pred.get("ground_contraindication") in (None, "")
    return out


def field_accuracy(scored):
    """scored: [{field: bool}] -> {field: accuracy, "overall": mean}."""
    if not scored:
        return {}
    out = {f: round(sum(s[f] for s in scored) / len(scored), 4) for f in EXTRACT_FIELDS}
    out["overall"] = round(statistics.mean(out[f] for f in EXTRACT_FIELDS), 4)
    return out


# ------------------------------------------------------------ redaction
def redacted_ranges(raw, deid):
    """Character ranges of `raw` that redaction replaced with [TAG]s. The
    de-identified text is raw text with spans swapped for tags, so the text
    between tags is matched back verbatim (anchored regex); difflib is the
    fallback when that alignment fails."""
    parts = TAG.split(deid)
    if len(parts) > 1:
        pat = "(.+?)".join(re.escape(p) for p in parts)
        try:
            m = re.fullmatch(pat, raw, re.S)
        except (re.error, RecursionError):
            m = None
        if m:
            return [m.span(i) for i in range(1, len(parts))]
    elif deid == raw:
        return []
    sm = difflib.SequenceMatcher(None, raw, deid, autojunk=False)
    out = [(i1, i2) for op, i1, i2, _, _ in sm.get_opcodes() if op in ("replace", "delete") and i2 > i1]
    merged = []
    for a, b in out:
        if merged and not raw[merged[-1][1]:a].strip(" ,.-/"):
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def _covered(span, ranges, text):
    a, b = span
    need = {i for i in range(a, b) if not text[i].isspace() and text[i] not in ",.-/()"}
    got = set()
    for x, y in ranges:
        got |= set(range(max(a, x), min(b, y)))
    return need <= got


def score_redaction(raw, deid, truth):
    """truth: [{"phi_type", "value", "span_start", "span_end"}] for one doc.
    Returns {"by_type": {type: [found, total]}, "tp": n, "redactions": n}.
    A truth identifier is found when every character of its span was
    redacted (or, with no span, when its value no longer appears)."""
    ranges = redacted_ranges(raw, deid)
    by_type = {}
    spans = []
    for t in truth:
        k = t["phi_type"]
        if t.get("span_start") is not None and t.get("span_end") is not None:
            sp = (int(t["span_start"]), int(t["span_end"]))
            spans.append(sp)
            ok = _covered(sp, ranges, raw)
        else:
            ok = t["value"] not in deid
        f, n = by_type.get(k, [0, 0])
        by_type[k] = [f + int(ok), n + 1]
    tp = sum(1 for x, y in ranges if any(x < b and a < y for a, b in spans))
    return {"by_type": by_type, "tp": tp, "redactions": len(ranges)}


def redaction_summary(per_doc):
    by_type, tp, red = {}, 0, 0
    for d in per_doc:
        for k, (f, n) in d["by_type"].items():
            a = by_type.setdefault(k, [0, 0])
            a[0] += f
            a[1] += n
        tp += d["tp"]
        red += d["redactions"]
    found, total = sum(v[0] for v in by_type.values()), sum(v[1] for v in by_type.values())
    return {"recall_by_type": {k: round(f / n, 4) for k, (f, n) in sorted(by_type.items())},
            "recall": round(found / total, 4) if total else None,
            "precision": round(tp / red, 4) if red else None,
            "leaked": total - found, "identifiers": total, "redactions": red}


# ------------------------------------------------------------ reconciliation
def score_reconcile(pred, truth, version=None):
    """pred: {"at_risk", "policy_doc_id", "policy_section", "policy_effective_date"};
    truth: {"at_risk", "doc_id", "section"}; version: optional
    {"doc_id", "effective_date"} for the policy version that truly applied."""
    out = {"at_risk": bool(pred.get("at_risk")) == bool(truth.get("at_risk"))}
    if truth.get("at_risk") and truth.get("doc_id"):
        out["policy"] = (pred.get("policy_doc_id") == truth.get("doc_id")
                         and (not truth.get("section") or pred.get("policy_section") == truth.get("section")))
    if version:
        ok = True
        if version.get("doc_id"):
            ok = ok and pred.get("policy_doc_id") == version["doc_id"]
        if version.get("effective_date"):
            ok = ok and str(pred.get("policy_effective_date"))[:10] == str(version["effective_date"])[:10]
        out["policy_version"] = ok
    return out


def rate(items, key):
    vals = [i[key] for i in items if key in i]
    return round(sum(vals) / len(vals), 4) if vals else None


# ------------------------------------------------------------ retrieval
def retrieval_hit(hits, gold, forbid=(), expect_none=False, k=5):
    """hits: [[doc_id, section, date]] in rank order. gold: [[doc_id, section
    or None]]. A hit needs any gold in the top k and no forbidden doc there;
    with no gold, a hit means no forbidden doc (a trap); expect_none means
    nothing should be returned at all. Returns (hit, recall, forbidden_seen)."""
    top = hits[:k]
    bad = any(h[0] in set(forbid) for h in top)
    if expect_none:
        return (not top, 1.0 if not top else 0.0, bad)
    if not gold:
        return (not bad, 1.0 if not bad else 0.0, bad)
    found = [g for g in gold if any(h[0] == g[0] and (len(g) < 2 or not g[1] or h[1] == g[1]) for h in top)]
    return (bool(found) and not bad, round(len(found) / len(gold), 4), bad)


# ------------------------------------------------------------ answers
def number_present(answer_numbers, value):
    """answer_numbers: [(tok, value, decimals, is_pct)] from answer.check.numbers."""
    v = abs(float(value))
    for _tok, x, dec, pct in answer_numbers:
        x = abs(x)
        if abs(x - v) < 1e-9 or abs(round(v, dec) - x) < 1e-9 or (pct and abs(round(v * 100, dec) - x) < 1e-9):
            return True
    return False


def numeric_correctness(answer_numbers, expected):
    if not expected:
        return None
    return round(sum(number_present(answer_numbers, e) for e in expected) / len(expected), 4)


def median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None
