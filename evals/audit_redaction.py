"""Measure identifier removal in the billing view against synthetic span labels."""
import argparse
import json
from collections import defaultdict

import duckdb

from evals.metrics import redaction_summary, score_redaction
from waves.framework import fetch


def audit(db):
    labels = defaultdict(list)
    for row in fetch(db, "SELECT * FROM bronze.truth_phi"):
        labels[row["doc_id"]].append(row)
    scored = []
    unavailable = 0
    rows = db.execute("""
        SELECT raw.doc_id, raw.raw_text, published.deid_text FROM bronze.epcrs raw
        LEFT JOIN role_billing.epcr_deid published USING (doc_id)
        ORDER BY raw.doc_id
    """).fetchall()
    for doc_id, raw, clean in rows:
        if clean is None:
            unavailable += 1
            continue
        scored.append(score_redaction(raw, clean, labels[doc_id]))
    return {"charts": len(rows), "unavailable": unavailable, **redaction_summary(scored)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("warehouse")
    args = parser.parse_args()
    with duckdb.connect(args.warehouse, read_only=True) as db:
        result = audit(db)
    print(json.dumps(result, indent=2))
    return int(not result["identifiers"] or result["unavailable"] > 0 or result["leaked"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
