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
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from redact import normalize_column_name  # noqa: E402


def _is_sensitive(column_name: str, sensitive_columns: list[dict]) -> bool:
    normalized = normalize_column_name(column_name)
    return any(re.search(rule["pattern"], normalized) for rule in sensitive_columns)


def _matches_hash_pattern(column_name: str, hash_patterns: list[str]) -> bool:
    normalized = normalize_column_name(column_name)
    return any(re.search(pattern, normalized) for pattern in hash_patterns)


def build_transform_spec(contract: dict, table_name: str, sensitive_columns: list[dict] = None,
                          pii_target_transform: dict = None) -> dict:
    """sensitive_columns is toolkit.yaml's sample_data.sensitive_columns (used only to detect
    which columns ARE pii -- it never itself drives a real-data transform). pii_target_transform
    is toolkit.yaml's pii_handling.target_transform ({enabled, hash_patterns}) -- the separate,
    opt-in policy for what actually happens to those columns in the real generated target. See
    DECISIONS.md for why these are two different config surfaces rather than one."""
    sensitive_columns = sensitive_columns or []
    pii_target_transform = pii_target_transform or {}
    transform_enabled = pii_target_transform.get("enabled", False)
    hash_patterns = pii_target_transform.get("hash_patterns", [])

    table = next((t for t in contract["tables"] if t["name"] == table_name), None)
    if table is None:
        raise ValueError(f"No table '{table_name}' in this data-contract. Available: "
                          f"{[t['name'] for t in contract['tables']]}")

    columns = []
    target_transform_gaps = []
    source_objects = set()
    for col in table["columns"]:
        src = col["source"]
        source_objects.add(src["object"])

        is_sensitive = _is_sensitive(src["column"], sensitive_columns)
        hashed = transform_enabled and _matches_hash_pattern(src["column"], hash_patterns)
        target_transform = "hash" if hashed else None
        if is_sensitive and not hashed:
            reason = ("pii_handling.target_transform.enabled is false" if not transform_enabled
                      else "no hash_pattern in toolkit.yaml matched this column -- sample "
                           "redaction only, real target untransformed")
            target_transform_gaps.append({"column": col["name"], "reason": reason})

        columns.append({
            "target": col["name"],
            "source_column": src["column"],
            "type": col["type"],
            "nullable": col["nullable"],
            "mapping_type": src["mapping_type"],
            "mapping_confidence": src.get("confidence"),
            "target_transform": target_transform,
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
        "target_transform_gaps": target_transform_gaps,
    }


def render_select_sql(spec: dict, source_ref: str = None) -> str:
    """Portable SELECT (SQLite-compatible, close enough to Spark SQL for the generated
    templates). source_ref lets callers point at a scratch/mock table name instead of
    schema.table when testing locally against mock data.

    A column with target_transform "hash" is wrapped in toolkit_hash(...) -- a placeholder name,
    not real SHA-256 syntax, since SQLite has no built-in hash function. validate_pipeline_locally.py
    registers it as a custom scalar function before this SQL runs; it only needs to be deterministic
    (same input -> same output across both idempotency-proof runs), not byte-identical to the real
    Spark F.sha2(...) generate_pipeline_code.py renders for the actual pipeline code."""
    src = source_ref or f"{spec['source_schema']}.{spec['source_table']}"

    def _select_expr(c: dict) -> str:
        if c.get("target_transform") == "hash":
            return f"toolkit_hash({c['source_column']}) AS {c['target']}"
        return f"{c['source_column']} AS {c['target']}"

    select_list = ", ".join(_select_expr(c) for c in spec["columns"])
    return f"SELECT {select_list} FROM {src}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--contract-json", type=Path, required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--sensitive-columns-json", type=Path, default=None,
                         help="JSON file: toolkit.yaml's sample_data.sensitive_columns list")
    parser.add_argument("--pii-target-transform-json", type=Path, default=None,
                         help="JSON file: toolkit.yaml's pii_handling.target_transform object")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    contract = json.loads(args.contract_json.read_text())
    sensitive_columns = (json.loads(args.sensitive_columns_json.read_text())
                         if args.sensitive_columns_json else [])
    pii_target_transform = (json.loads(args.pii_target_transform_json.read_text())
                            if args.pii_target_transform_json else {})
    try:
        spec = build_transform_spec(contract, args.table, sensitive_columns=sensitive_columns,
                                     pii_target_transform=pii_target_transform)
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
