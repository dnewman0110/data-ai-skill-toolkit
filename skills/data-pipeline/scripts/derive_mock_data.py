#!/usr/bin/env python3
"""
derive_mock_data.py -- synthesizes a small mock dataset for a data-contract table's SOURCE
object, used for local idempotency testing (validate_pipeline_locally.py) and as fixture data a
human can inspect before anything runs against a real target. Purely synthetic: derived only from
the contract's declared types/nullability/tests/sample_records, never from a live read of client
data, so it can't violate client data isolation (references/toolkit-conventions.md #3) even by
accident -- there is no client data in this script's inputs at all.

Determinism: seeded (default seed 1337) so the same contract always produces the same mock
dataset -- a re-run's idempotency check is comparing the pipeline logic, not incidentally
comparing two different random datasets.

v1 simplification: mock rows are keyed by the SOURCE column name (so the transform spec's SELECT
can run against them unmodified) but shaped using the TARGET column's declared type/nullability,
since that's what the contract records. Where source and target types genuinely differ (the
fixture lakehouse's own planted source/target type mismatch -- silver.orders.total_amt is TEXT,
fct_orders.order_total_usd is decimal -- is a real example), the mock data will NOT reproduce that
mismatch; it reflects the target's declared shape, not the source's actual one. This is a
documented limitation, not a silent guess -- see references/idempotency-and-mock-data.md.
"""
import argparse
import json
import random
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path


def _null_rate_for_column(col: dict, tests: list[dict]) -> float:
    if not col["nullable"]:
        return 0.0
    for t in tests:
        if t["type"] == "nullability" and t["column"] == col["name"]:
            max_rate = t.get("params", {}).get("max_null_rate")
            if max_rate is not None:
                return float(max_rate)
    return 0.05  # default: nullable columns with no explicit test get a light null rate


def _gen_value(col: dict, row_index: int, is_key: bool, rng: random.Random):
    t = col["type"].lower()
    name = col["name"].lower()
    if is_key or re.search(r"(^|_)id$", name):
        # Key/id-shaped columns: base + row_index guarantees uniqueness across the mock set.
        return 100000 + row_index
    if re.match(r"^(big)?int(eger)?$|^smallint$|^tinyint$", t):
        return rng.randint(1, 500)
    if t.startswith("decimal") or t in ("double", "float", "real"):
        return round(rng.uniform(1.0, 500.0), 2)
    if t == "boolean":
        return rng.choice([True, False])
    if t in ("date",):
        d = datetime(2025, 1, 1) + timedelta(days=rng.randint(0, 550))
        return d.strftime("%Y-%m-%d")
    if t.startswith("timestamp"):
        d = datetime(2025, 1, 1) + timedelta(days=rng.randint(0, 550), seconds=rng.randint(0, 86399))
        return d.strftime("%Y-%m-%dT%H:%M:%SZ")
    # string / varchar / anything unrecognized
    return f"{name}_{row_index}"


def derive_mock_data(table: dict, row_count: int = 50, seed: int = 1337) -> list[dict]:
    """Returns rows keyed by SOURCE column name (see module docstring for why)."""
    rng = random.Random(seed)
    tests = table.get("tests", [])
    uniqueness_tests = [t for t in tests if t["type"] == "uniqueness"]
    key_columns = set()
    for t in uniqueness_tests:
        key_columns.update(t.get("params", {}).get("columns", [t["column"]]))

    null_rates = {c["name"]: _null_rate_for_column(c, tests) for c in table["columns"]}

    rows = []
    for i in range(row_count):
        row = {}
        for col in table["columns"]:
            name = col["name"]  # target name, used only to look up type/nullability/null-rate
            source_col = col["source"]["column"]
            if rng.random() < null_rates.get(name, 0.0):
                row[source_col] = None
                continue
            row[source_col] = _gen_value(col, i, name in key_columns, rng)
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--contract-json", type=Path, required=True)
    parser.add_argument("--table", required=True, help="Target table name in the contract (mock data is generated shaped like that table's declared columns).")
    parser.add_argument("--row-count", type=int, default=50)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    contract = json.loads(args.contract_json.read_text())
    table = next((t for t in contract["tables"] if t["name"] == args.table), None)
    if table is None:
        print(f"ERROR: no table '{args.table}' in contract.", file=sys.stderr)
        sys.exit(1)

    rows = derive_mock_data(table, row_count=args.row_count, seed=args.seed)
    args.out.write_text(json.dumps(rows, indent=2))
    print(f"Wrote {len(rows)} mock rows to {args.out}")
    sys.exit(0)


if __name__ == "__main__":
    main()
