"""Run every wave in order, then build gold and the role schemas. Idempotent:
each step replaces its output table.

    python -m waves.pipeline [--db warehouse.duckdb] [--provider wave1.billing=local ...]

--provider TASK_PREFIX=PROVIDER picks the provider for every task under the
prefix (wave1, wave1.billing, wave1.billing.extract ...); the longest prefix
wins and everything else runs on rules. Providers: rules, local, claude,
codex, gemini. Raw-PHI tasks (wave1.billing.redact_names) accept only local or
rules; a cloud provider there falls back to rules.
"""
import argparse
import datetime as dt
import sys
from pathlib import Path

import duckdb

from waves import billing, finance, hr, ops
from waves.framework import Ctx, ensure_schemas

REPO = Path(__file__).resolve().parent.parent

STEPS = [
    billing.redact_step, billing.extract_step,          # wave 1
    ops.classify_step, ops.index_step,
    finance.normalize_step,
    hr.licenses_step, hr.overtime_step,
    billing.reconcile_step,                              # wave 2
    ops.aog_impact_step,
    hr.compliance_step, hr.aog_overtime_step,
    finance.variance_step,                               # needs ops + hr wave 2 drivers
]


def parse_providers(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"--provider wants TASK=PROVIDER, got {it!r}")
        k, v = it.split("=", 1)
        if v not in ("rules", "local", "claude", "codex", "gemini"):
            raise SystemExit(f"unknown provider {v!r}")
        out[k.strip()] = v.strip()
    return out


def run(con, ctx=None, gold=True, governance=True, only=None, echo=None):
    from gold import build as gold_build
    from governance import roles

    ctx = ctx or Ctx()
    ensure_schemas(con)
    results = []
    for s in STEPS:
        if only and not any(s.step_name.startswith(o) for o in only):
            continue
        r = s(con, ctx)
        results.append(r)
        if echo:
            echo(r.line())
    if gold:
        n = gold_build.build(con)
        if echo:
            echo(f"gold: entity_links={n}, metric views from gold/metrics.sql")
    if governance:
        roles.apply(con)
        if echo:
            echo(f"governance: role schemas {', '.join('role_' + x for x in roles.ROLES)}")
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(REPO / "warehouse.duckdb"))
    ap.add_argument("--provider", action="append", default=[], metavar="TASK=PROVIDER")
    ap.add_argument("--model", action="append", default=[], metavar="PROVIDER=MODEL")
    ap.add_argument("--as-of", default=None, help="reference date for license status (default: last shift date)")
    ap.add_argument("--only", action="append", default=None, help="run only steps with this name prefix")
    a = ap.parse_args(argv)
    ctx = Ctx(providers=parse_providers(a.provider),
              models=dict(m.split("=", 1) for m in a.model),
              as_of=dt.date.fromisoformat(a.as_of) if a.as_of else None)
    if not Path(a.db).exists():
        print(f"{a.db} not found: run `python -m data_gen.generate_all --seed 42 --out {a.db}` first", file=sys.stderr)
        return 2
    con = duckdb.connect(a.db)
    try:
        run(con, ctx, gold=not a.only, governance=not a.only, only=a.only, echo=print)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
