#!/usr/bin/env python3
"""
detect_scd_candidates.py -- deterministic signal for SCD type 2 material: does a sibling
"history" table already exist for this dimension's source object? A table named
<source_table>_history / <source_table>_hist / <source_table>_scd in the SAME schema is strong,
measured evidence that whoever built the source layer already recognized an attribute changes
over time and is tracking it -- e.g. silver.customer_region_history existing alongside
silver.customers is exactly the "region has been reassigned before, and someone cared enough to
track it" signal that makes customers.region a real SCD type 2 candidate, not just a guess.

What this does NOT do: decide the scd_type. A history table's existence is evidence an attribute
CAN change and that change is meaningful to track -- it is not proof every attribute on the
dimension needs type 2, and its ABSENCE does not prove an attribute is safely type 1 (a team might
simply not have built history tracking yet for something that should have it). The agent still
writes scd_type + scd_rationale per attribute, grounded in this evidence plus the business context
gathered per SKILL.md step 1 -- see references/scd-type-selection.md.
Matching is deliberately loose, not an exact "<table>_history" match: real schemas name history
tables things like "customer_region_history" (singular stem + a domain word + suffix), not
"customers_history". A table counts as a candidate if it ends in a history-shaped suffix AND
contains the source table's singular stem -- a documented heuristic, not a fuzzy-match claiming
certainty (see the "history_tables_found" evidence in the output; the agent still confirms each
match names a genuinely related attribute before treating it as SCD 2 evidence).
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lakehouse_adapter import LakehouseAdapter, SQLiteFixtureAdapter  # noqa: E402

HISTORY_TABLE_SUFFIXES = ("_history", "_hist", "_scd")


def _singular_stem(table: str) -> str:
    return table[:-1] if table.endswith("s") and not table.endswith("ss") else table


def detect_scd_candidates(adapter: LakehouseAdapter, schema: str, table: str) -> dict:
    all_tables = adapter.list_tables(schema)
    stem = _singular_stem(table)
    candidates = [
        t for t in all_tables
        if t != table and stem in t and any(t.endswith(suffix) for suffix in HISTORY_TABLE_SUFFIXES)
    ]
    result = {"object": f"{schema}.{table}", "history_tables_found": candidates, "columns_in_history_tables": {}}
    for hist_table in candidates:
        cols = [c.name for c in adapter.get_columns(schema, hist_table)]
        result["columns_in_history_tables"][hist_table] = cols
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lakehouse-dir", required=True)
    parser.add_argument("--catalog", default="acme_retail_dev")
    parser.add_argument("--schema", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    adapter = SQLiteFixtureAdapter(args.lakehouse_dir, catalog=args.catalog)
    result = detect_scd_candidates(adapter, args.schema, args.table)
    output = json.dumps(result, indent=2, default=str)
    if args.out:
        args.out.write_text(output)
    else:
        print(output)
    sys.exit(0)


if __name__ == "__main__":
    main()
