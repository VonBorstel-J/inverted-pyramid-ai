"""Generate a complete bronze snapshot in one transaction."""
import argparse
from pathlib import Path

import duckdb
import pyarrow as pa

from . import billing, finance, hr, ops
from .common import Dataset, SCHEMAS


def generate(seed, out):
    data = Dataset(seed)
    for dept in (ops, billing, hr, finance):
        dept.generate(data)
    counts = {}
    with duckdb.connect(str(out)) as con:
        con.execute("BEGIN")
        for namespace in ("bronze", "silver", "gold"):
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {namespace}")
        for name, columns in SCHEMAS.items():
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
    args = parser.parse_args()
    counts = generate(args.seed, args.out)
    print("Bronze table                    Rows")
    for name, count in sorted(counts.items()):
        print(f"{name:30} {count:5}")


if __name__ == "__main__":
    main()
