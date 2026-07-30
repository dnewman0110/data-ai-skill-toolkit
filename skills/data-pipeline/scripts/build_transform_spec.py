#!/usr/bin/env python3
"""
build_transform_spec.py -- derives a portable, modality-agnostic transform spec for ONE target
table from a data-contract.json (or, for a target with no upstream contract yet, this script is
not used -- data-pipeline requires at least a data-contract per source_refs; see SKILL.md step 1).

"Portable" means: a straight column mapping (target <- source object.column) plus a merge-key
list, expressed independently of any modality. The same spec renders to a SQL SELECT for local
idempotency testing (render_select_sql below) AND is the input generate_pipeline_code.py uses to
produce PySpark notebook / Declarative Pipeline / Lakeflow Connect code -- one spec, multiple
targets, so the generated code and the tested logic can never silently diverge.

v1 scope limitation: single-source targets only (one distinct source object across all of a
table's columns). A target whose columns come from more than one source object needs a join,
which is exactly the kind of logic references/decision-rubric.md classifies as
transform_complexity: complex_procedural -- hand-author it as a pyspark_notebook and note the gap
in assumptions[] rather than pretending this script can derive it. See DECISIONS.md.
"""
import argparse
import json
import sys
from pathlib import Path


def build_transform_spec(contract: dict, table_name: str) -> dict:
    table = next((t for t in contract["tables"] if t["name"] == table_name), None)
    if table is None:
        raise ValueError(f"No table '{table_name}' in this data-contract. Available: "
                          f"{[t['name'] for t in contract['tables']]}")

    columns = []
    source_objects = set()
    for col in table["columns"]:
        src = col["source"]
        source_objects.add(src["object"])
        columns.append({
            "target": col["name"],
            "source_column": src["column"],
            "type": col["type"],
            "nullable": col["nullable"],
            "mapping_type": src["mapping_type"],
            "mapping_confidence": src.get("confidence"),
        })

    if len(source_objects) != 1:
        raise ValueError(
            f"build_transform_spec only supports single-source targets in v1; '{table_name}' maps "
            f"from {sorted(source_objects)}. Multi-source joins require hand-authoring a "
            f"pyspark_notebook -- see references/other-modalities.md."
        )
    source_object = source_objects.pop()
    parts = source_object.split(".")
    if len(parts) != 3:
        raise ValueError(f"Expected catalog.schema.table, got '{source_object}'.")
    source_catalog, source_schema, source_table = parts

    # Merge keys: prefer an explicit uniqueness test's params.columns (a real, executable column
    # list) over table.grain.statement (prose, not meant to be parsed).
    uniqueness_tests = [t for t in table.get("tests", []) if t["type"] == "uniqueness"]
    merge_keys = []
    if uniqueness_tests and "columns" in uniqueness_tests[0].get("params", {}):
        merge_keys = list(uniqueness_tests[0]["params"]["columns"])

    low_confidence_mappings = [
        c["target"] for c in columns
        if c["mapping_type"] == "llm_inferred" and (c["mapping_confidence"] or 0) < 0.5
    ]

    return {
        "target_table": table_name,
        "target_catalog": table["target_catalog"],
        "target_schema": table["target_schema"],
        "source_catalog": source_catalog,
        "source_schema": source_schema,
        "source_table": source_table,
        "merge_keys": merge_keys,
        "load_pattern": "merge_upsert" if merge_keys else "full_refresh",
        "columns": columns,
        "tests": table.get("tests", []),
        "low_confidence_mappings": low_confidence_mappings,
    }


def render_select_sql(spec: dict, source_ref: str = None) -> str:
    """Portable SELECT (SQLite-compatible, close enough to Spark SQL for the generated
    templates). source_ref lets callers point at a scratch/mock table name instead of
    schema.table when testing locally against mock data."""
    src = source_ref or f"{spec['source_schema']}.{spec['source_table']}"
    select_list = ", ".join(f"{c['source_column']} AS {c['target']}" for c in spec["columns"])
    return f"SELECT {select_list} FROM {src}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--contract-json", type=Path, required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    contract = json.loads(args.contract_json.read_text())
    try:
        spec = build_transform_spec(contract, args.table)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    output = json.dumps(spec, indent=2)
    if args.out:
        args.out.write_text(output)
        print(f"Transform spec written to {args.out}")
    else:
        print(output)
    sys.exit(0)


if __name__ == "__main__":
    main()
