"""Step framework: `run(con, ctx) -> StepResult`, provenance columns, and the
pydantic schema gate that writes good rows and quarantines bad ones into
`<table>_rejects` with the reason (ARCHITECTURE.md section 4)."""
import datetime as dt
import json
import time
import types
import typing
from dataclasses import dataclass, field

import pandas as pd
from pydantic import BaseModel, ValidationError

PROV_COLS = ("prov_source", "prov_wave", "prov_step", "prov_provider", "prov_model",
             "prov_prompt_version", "prov_at")
PROV_DDL = ("prov_source VARCHAR, prov_wave INTEGER, prov_step VARCHAR, prov_provider VARCHAR, "
            "prov_model VARCHAR, prov_prompt_version VARCHAR, prov_at TIMESTAMP")
REJECT_DDL = "prov_source VARCHAR, reason VARCHAR, payload VARCHAR, prov_step VARCHAR, prov_at TIMESTAMP"


@dataclass
class Ctx:
    """Run context. `providers` maps a task prefix to a provider, for example
    {"wave1.billing": "local"}; the longest matching prefix wins, default rules."""
    providers: dict = field(default_factory=dict)
    models: dict = field(default_factory=dict)
    run_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0))
    as_of: typing.Optional[dt.date] = None
    llm_opts: dict = field(default_factory=dict)
    verbose: bool = False

    def provider(self, task):
        best, n = "rules", -1
        for k, v in self.providers.items():
            if (task == k or task.startswith(k + ".")) and len(k) > n:
                best, n = v, len(k)
        return best

    def model(self, provider):
        return self.models.get(provider)


@dataclass
class StepResult:
    step: str
    wave: int
    table: str
    rows_in: int = 0
    rows_out: int = 0
    rows_rejected: int = 0
    provider: str = "rules"
    ms: float = 0.0
    notes: list = field(default_factory=list)

    def line(self):
        n = f"  ({'; '.join(self.notes)})" if self.notes else ""
        return (f"wave{self.wave} {self.step:<26} -> {self.table:<32} in={self.rows_in:<5} out={self.rows_out:<5} "
                f"rejects={self.rows_rejected:<4} provider={self.provider} {self.ms:.0f}ms{n}")


# ---------------------------------------------------------------- helpers

def has_table(con, qualified):
    schema, name = qualified.split(".")
    return con.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema=? AND table_name=?",
                       [schema, name]).fetchone()[0] > 0


def columns(con, qualified):
    schema, name = qualified.split(".")
    return [r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema=? AND table_name=? ORDER BY ordinal_position",
        [schema, name]).fetchall()]


def fetch(con, sql, params=None):
    cur = con.execute(sql, params or [])
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def ensure_schemas(con):
    for s in ("bronze", "silver", "gold"):
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")


_TYPES = {str: "VARCHAR", int: "BIGINT", float: "DOUBLE", bool: "BOOLEAN",
          dt.date: "DATE", dt.datetime: "TIMESTAMP"}


def _duck_type(ann):
    origin = typing.get_origin(ann)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        return _duck_type(args[0]) if len(args) == 1 else "VARCHAR"
    if origin is typing.Literal:
        return _duck_type(type(typing.get_args(ann)[0]))
    if origin in (list, dict) or ann in (list, dict):
        return "VARCHAR"  # stored as JSON text
    return _TYPES.get(ann, "VARCHAR")


def ddl(model):
    cols = [f'"{n}" {_duck_type(f.annotation)}' for n, f in model.model_fields.items()]
    return ", ".join(cols + [PROV_DDL])


def provenance(source, wave, step, provider="rules", model=None, prompt_version=None, at=None):
    return {"prov_source": source, "prov_wave": wave, "prov_step": step, "prov_provider": provider,
            "prov_model": model or ("sql" if provider == "sql" else provider),
            "prov_prompt_version": prompt_version, "prov_at": at}


def gate(con, table, model, records, result, at):
    """Validate each (row_dict, prov_dict) pair; write good rows to `table`
    (replaced: steps are idempotent) and the rest to `table_rejects`."""
    good, bad = [], []
    fields = list(model.model_fields)
    for row, prov in records:
        prov = dict(prov, prov_at=prov.get("prov_at") or at)
        if "_reject" in row:
            bad.append({"prov_source": prov["prov_source"], "reason": row["_reject"],
                        "payload": json.dumps(row.get("_payload"), default=str)[:4000],
                        "prov_step": prov["prov_step"], "prov_at": prov["prov_at"]})
            continue
        try:
            m = model.model_validate(row)
        except ValidationError as e:
            bad.append({"prov_source": prov["prov_source"],
                        "reason": "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())[:1000],
                        "payload": json.dumps(row, default=str)[:4000], "prov_step": prov["prov_step"],
                        "prov_at": prov["prov_at"]})
            continue
        d = m.model_dump()
        for k, v in d.items():
            if isinstance(v, (list, dict)):
                d[k] = json.dumps(v, default=str)
        good.append({**d, **{c: prov.get(c) for c in PROV_COLS}})
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {table.split('.')[0]}")
    con.execute(f"CREATE OR REPLACE TABLE {table} ({ddl(model)})")
    con.execute(f"CREATE OR REPLACE TABLE {table}_rejects ({REJECT_DDL})")
    cols = fields + list(PROV_COLS)
    if good:
        df = pd.DataFrame(good, columns=cols)
        con.register("_gate_df", df)
        con.execute(f"INSERT INTO {table} SELECT {', '.join(chr(34) + c + chr(34) for c in cols)} FROM _gate_df")
        con.unregister("_gate_df")
    if bad:
        df = pd.DataFrame(bad)
        con.register("_gate_bad", df)
        con.execute(f"INSERT INTO {table}_rejects SELECT prov_source, reason, payload, prov_step, prov_at FROM _gate_bad")
        con.unregister("_gate_bad")
    result.rows_out, result.rows_rejected = len(good), len(bad)
    return result


class Timer:
    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *a):
        self.ms = (time.perf_counter() - self.t0) * 1000


def step(name, wave, table):
    """Decorator: wraps `fn(con, ctx, result) -> None` into `run(con, ctx) -> StepResult`."""
    def deco(fn):
        def run(con, ctx):
            r = StepResult(step=name, wave=wave, table=table)
            with Timer() as t:
                fn(con, ctx, r)
            r.ms = t.ms
            return r
        run.step_name, run.wave, run.table, run.__doc__ = name, wave, table, fn.__doc__
        return run
    return deco
