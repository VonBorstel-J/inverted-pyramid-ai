"""Local vs cloud: the same evals, same charts and questions, per provider.

    python -m evals.compare --split hard --limit 200 --providers rules,local,claude [--start-servers]

Each provider runs Wave 1 billing extraction (and answer writing) through the
router; `claude` is `claude -p` (model haiku unless IPA_CLAUDE_MODEL says
otherwise) and only ever sees de-identified text: the router's PHI guard
refuses anything with an identifier pattern, and raw-PHI name finding stays
on rules for any non-local provider. Writes
evals/results/<timestamp>-<split>-compare.json and prints the table.
"""
import argparse
import datetime as dt
import json
from pathlib import Path

from evals import run as R


def compare(split, providers, limit, answers=None, db=None, start_servers=False, write_quality=True, echo=print):
    results = []
    for i, p in enumerate(providers):
        echo(f"== provider {p}")
        results.append(R.run(split, R.parse_providers([f"wave1.billing={p}"]), limit, db, answers,
                             # warm embeddings once; only `local` then needs the chat server
                             start_servers and (i == 0 or p == "local"), write_quality, echo=echo))
    table = R.summary_table(results)
    out = {"split": split, "limit": limit, "providers": providers, "table": table,
           "summary": [R.summary_row(r) for r in results], "results": [r["path"] for r in results]}
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(R.HERE / "results" / f"{stamp}-{split}-compare.json")
    path.write_text(json.dumps(out, indent=1, default=str))
    echo("\n" + table)
    echo(f"comparison: {path}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", default="hard", choices=sorted(R.DBS))
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--providers", default="rules,local,claude")
    ap.add_argument("--answers", type=int, default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("--start-servers", action="store_true")
    ap.add_argument("--no-quality", action="store_true")
    a = ap.parse_args(argv)
    compare(a.split, [p.strip() for p in a.providers.split(",") if p.strip()], a.limit, a.answers, a.db,
            a.start_servers, not a.no_quality)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
