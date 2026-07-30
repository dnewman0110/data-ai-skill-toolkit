#!/usr/bin/env python3
"""
derive_conformance_candidates.py -- deterministic signal for whether a proposed dimension should
be `kind: conformed` and reuse an existing one, rather than `kind: local` freshly designed. Scans
the target gold schema for tables matching common dimension-naming conventions
(dim_<name>, <name>_dim) whose name is a plausible match for a proposed dimension name (via
substring/normalized comparison, not a fuzzy-match guess dressed as certainty). Existing matches
are reported as candidates, not automatically merged in -- see references/conformed-dimensions.md
for why the agent still confirms conformance groups explicitly rather than this script asserting
one.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lakehouse_adapter import LakehouseAdapter, SQLiteFixtureAdapter  # noqa: E402


def _normalize(name: str) -> str:
    name = re.sub(r"^dim_|_dim$", "", name.lower())
    return name.strip("_")


def derive_conformance_candidates(adapter: LakehouseAdapter, gold_schema: str, proposed_dimension_names: list[str]) -> dict:
    try:
        existing_tables = adapter.list_tables(gold_schema)
    except FileNotFoundError:
        existing_tables = []
    dim_shaped = [t for t in existing_tables if t.lower().startswith("dim_") or t.lower().endswith("_dim")]

    candidates = {}
    for proposed in proposed_dimension_names:
        norm_proposed = _normalize(proposed)
        matches = [t for t in dim_shaped if _normalize(t) == norm_proposed]
        candidates[proposed] = matches

    return {"gold_schema": gold_schema, "existing_dimension_shaped_tables": dim_shaped, "candidates": candidates}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lakehouse-dir", required=True)
    parser.add_argument("--catalog", default="acme_retail_dev")
    parser.add_argument("--gold-schema", default="gold")
    parser.add_argument("--dimension-name", action="append", required=True, help="repeatable: proposed dimension name(s)")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    adapter = SQLiteFixtureAdapter(args.lakehouse_dir, catalog=args.catalog)
    result = derive_conformance_candidates(adapter, args.gold_schema, args.dimension_name)
    output = json.dumps(result, indent=2, default=str)
    if args.out:
        args.out.write_text(output)
    else:
        print(output)
    sys.exit(0)


if __name__ == "__main__":
    main()
