"""Generate a complete bronze snapshot in one transaction."""
import argparse
from pathlib import Path

import duckdb
import pyarrow as pa

from . import billing, finance, hr, ops
from .common import Dataset, SCHEMAS, schema


def generate(seed, out, split="default"):
    if split not in ("hard", "default"):
        raise ValueError(f"Unknown split {split!r}")
    schemas = SCHEMAS.copy()
    if split == "hard":
        from . import hard_documents, hard_templates
        schemas["truth_policy_version"] = schema(
            "claim_id payer transport_date:DATE version:INTEGER doc_id effective_date:DATE")
    data = Dataset(seed, schemas)
    ops.generate(data)
    billing.generate(data, hard=split == "hard")
    hr.generate(data)
    finance.generate(data)
    if split == "hard":
        hard_templates.rewrite_duty(data)
        hard_documents.generate(data)
    counts = {}
    with duckdb.connect(str(out)) as con:
        con.execute("BEGIN")
        for namespace in ("bronze", "silver", "gold"):
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {namespace}")
        if split == "default":
            con.execute("DROP TABLE IF EXISTS bronze.truth_policy_version")
        for name, columns in schemas.items():
            rows = data.tables[name]
            batch = pa.table({col: [row[col] for row in rows] for col in columns})
            con.register("source_rows", batch)
            fields = ", ".join(f'CAST("{col}" AS {typ}) AS "{col}"' for col, typ in columns.items())
            con.execute(f"CREATE OR REPLACE TABLE bronze.{name} AS SELECT {fields} FROM source_rows")
            con.unregister("source_rows")
            counts[name] = len(rows)
        con.execute("COMMIT")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("warehouse.duckdb"))
    parser.add_argument("--split", choices=("default", "hard"), default="default")
    args = parser.parse_args()
    counts = generate(args.seed, args.out, args.split)
    print("Bronze table                    Rows")
    for name, count in sorted(counts.items()):
        print(f"{name:30} {count:5}")


if __name__ == "__main__":
    main()
