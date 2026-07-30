#!/usr/bin/env python3
"""
validate_grain_against_measures.py -- the deterministic half of a fact's
grain.validated_against_measures claim. Profiles whether the PROPOSED grain columns are actually
unique in the source object (the same profiled-uniqueness check data-discovery and
data-quality use, via LakehouseAdapter.check_uniqueness) and reports each measure's declared SQL
type, so the agent can classify additivity (additive/semi_additive/non_additive) grounded in a
real type, not a guess.

What this does NOT do: classify additivity itself. Whether "unit_price" is additive is a semantic
question a column's type can't answer (two order lines' unit prices don't sum to anything
meaningful; two order lines' extended totals do) -- see toolkit-conventions.md #5. This script
gives the agent the measured facts (grain uniqueness, declared type) it needs to make that call
and write a real validation_evidence string, not "trust me."
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lakehouse_adapter import LakehouseAdapter, SQLiteFixtureAdapter  # noqa: E402


def validate_grain(adapter: LakehouseAdapter, schema: str, table: str,
                    grain_columns: list[str], measure_columns: list[str]) -> dict:
    uniqueness = adapter.check_uniqueness(schema, table, grain_columns)
    total_rows = adapter.row_count(schema, table)
    columns_by_name = {c.name: c for c in adapter.get_columns(schema, table)}

    measures = []
    for m in measure_columns:
        col = columns_by_name.get(m)
        measures.append({
            "column": m,
            "exists": col is not None,
            "declared_type": col.type if col else None,
            "declared_nullable": col.nullable if col else None,
        })

    # checked_full_population isn't in check_uniqueness's return shape (that's profile_object.py's
    # own addition) -- compute it here the same way, so this script has no hidden dependency on
    # data-discovery's script.
    checked_full_population = uniqueness["rows_checked"] >= total_rows

    return {
        "object": f"{schema}.{table}",
        "grain_columns": grain_columns,
        "grain_uniqueness": {
            "rows_checked": uniqueness["rows_checked"],
            "rows_with_null_key": uniqueness["rows_with_null_key"],
            "distinct_count": uniqueness["distinct_count"],
            "is_unique": uniqueness["is_unique"],
            "duplicate_count": uniqueness["duplicate_count"],
            "checked_full_population": checked_full_population,
        },
        "grain_holds": bool(uniqueness["is_unique"]),
        "measures": measures,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lakehouse-dir", required=True)
    parser.add_argument("--catalog", default="acme_retail_dev")
    parser.add_argument("--schema", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--grain-column", action="append", required=True, help="repeatable")
    parser.add_argument("--measure-column", action="append", default=[], help="repeatable")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    adapter = SQLiteFixtureAdapter(args.lakehouse_dir, catalog=args.catalog)
    result = validate_grain(adapter, args.schema, args.table, args.grain_column, args.measure_column)

    output = json.dumps(result, indent=2, default=str)
    if args.out:
        args.out.write_text(output)
    else:
        print(output)
    sys.exit(0 if result["grain_holds"] else 1)


if __name__ == "__main__":
    main()
