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


# Deliberately coarse: bucket into broad categories rather than comparing type strings verbatim.
# Exact-string comparison would flag harmless cross-system spelling differences (INTEGER vs bigint,
# decimal(10,2) vs decimal(18,2)) on nearly every column. An unrecognized type on either side never
# matches any pattern below, so it never flags -- "never guess," applied in the safe direction: only
# flag a mismatch between two types this function is confident it understood.
_TYPE_CATEGORY_PATTERNS = [
    ("numeric", re.compile(r"^(tinyint|smallint|int|integer|bigint|long|decimal|numeric|double|float|real)\b")),
    ("string", re.compile(r"^(string|varchar|char|nvarchar|text)\b")),
    ("date", re.compile(r"^date\b")),
    ("timestamp", re.compile(r"^(timestamp|datetime)\b")),
    ("boolean", re.compile(r"^(boolean|bool)\b")),
    ("binary", re.compile(r"^(binary|varbinary)\b")),
]


def _type_category(type_str: str | None) -> str | None:
    if not type_str:
        return None
    normalized = type_str.strip().lower()
    for category, pattern in _TYPE_CATEGORY_PATTERNS:
        if pattern.match(normalized):
            return category
    return None


# A transformation like "DATEDIFF(check_out, check_in)" references a sibling source column
# (check_out) that may not itself be any column's mapped source_column in this table -- the real
# generated code still works fine (F.expr runs against the full source DataFrame, not just mapped
# columns), but the LOCAL mock/idempotency proof creates a narrow mock_source table shaped only by
# each column's own source_column, so it needs to know about these too. Best-effort identifier
# extraction (a denylist of common SQL keywords/functions), not real SQL parsing -- see
# DECISIONS.md. Never used to decide correctness, only to widen what mock data covers.
_SQL_KEYWORDS_AND_FUNCS = {
    "cast", "as", "and", "or", "not", "null", "is", "case", "when", "then", "else", "end",
    "date", "int", "integer", "bigint", "smallint", "tinyint", "decimal", "numeric",
    "double", "float", "real", "string", "varchar", "char", "boolean", "bool",
    "timestamp", "datetime", "binary", "true", "false",
    "datediff", "date_format", "date_add", "date_sub", "coalesce", "concat", "substring",
    "trim", "upper", "lower", "round", "abs", "year", "month", "day", "current_date",
    "current_timestamp",
}
_IDENTIFIER_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def _referenced_identifiers(expression: str) -> set[str]:
    return {tok for tok in _IDENTIFIER_RE.findall(expression) if tok.lower() not in _SQL_KEYWORDS_AND_FUNCS}


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
    type_mismatch_gaps = []
    scd2_columns = []
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

        transformation = src.get("transformation")
        source_type = src.get("source_type")
        if transformation is None and source_type is not None:
            target_category = _type_category(col["type"])
            source_category = _type_category(source_type)
            if target_category and source_category and target_category != source_category:
                type_mismatch_gaps.append({
                    "column": col["name"],
                    "target_type": col["type"],
                    "source_type": source_type,
                    "reason": f"declared type '{col['type']}' ({target_category}) doesn't match "
                              f"source type '{source_type}' ({source_category}) and no "
                              f"transformation is present -- a bare alias here would be silently "
                              f"wrong, not just imprecise.",
                })

        scd_type = col.get("scd_type")
        if scd_type == 2:
            scd2_columns.append(col["name"])

        columns.append({
            "target": col["name"],
            "source_column": src["column"],
            "type": col["type"],
            "nullable": col["nullable"],
            "mapping_type": src["mapping_type"],
            "mapping_confidence": src.get("confidence"),
            "target_transform": target_transform,
            "transformation": transformation,
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

    if scd2_columns and not merge_keys:
        raise ValueError(
            f"'{table_name}' has scd_type: 2 attribute(s) ({', '.join(scd2_columns)}) but no merge "
            f"keys -- SCD Type 2 history tracking has no key to track history against. Either "
            f"declare a uniqueness test's params.columns as the natural key, or reconsider whether "
            f"scd_type 2 is right for this target."
        )

    low_confidence_mappings = [
        c["target"] for c in columns
        if c["mapping_type"] == "llm_inferred" and (c["mapping_confidence"] or 0) < 0.5
    ]

    known_source_columns = {c["source_column"] for c in columns}
    extra_source_columns = set()
    for c in columns:
        if c.get("transformation"):
            extra_source_columns.update(_referenced_identifiers(c["transformation"]) - known_source_columns)

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
        "type_mismatch_gaps": type_mismatch_gaps,
        "stored_as_scd_type": 2 if scd2_columns else 1,
        "track_history_columns": scd2_columns,
        "extra_source_columns": sorted(extra_source_columns),
    }


def render_select_sql(spec: dict, source_ref: str = None) -> str:
    """Portable SELECT (SQLite-compatible, close enough to Spark SQL for the generated
    templates). source_ref lets callers point at a scratch/mock table name instead of
    schema.table when testing locally against mock data.

    A column with a non-null "transformation" (contract's source.transformation, e.g.
    "DATEDIFF(check_out, check_in)") is rendered as that expression rather than a bare column
    reference -- see references/toolkit-conventions.md and DECISIONS.md for why this must be a
    pure expression with no inline SQL comment (kimball-concepts.md tells data-modeling the same).
    On top of that, a column with target_transform "hash" is wrapped in toolkit_hash(...) -- a
    placeholder name, not real SHA-256 syntax, since SQLite has no built-in hash function.
    validate_pipeline_locally.py registers it as a custom scalar function before this SQL runs; it
    only needs to be deterministic (same input -> same output across both idempotency-proof runs),
    not byte-identical to the real Spark F.sha2(...)/F.expr(...) generate_pipeline_code.py renders
    for the actual pipeline code."""
    src = source_ref or f"{spec['source_schema']}.{spec['source_table']}"

    def _select_expr(c: dict) -> str:
        base = c.get("transformation") or c["source_column"]
        if c.get("target_transform") == "hash":
            base = f"toolkit_hash({base})"
        # Alias on its own line: if a transformation string ends up with a trailing "--" comment
        # anyway despite the "pure expression" guidance, it can only swallow the rest of ITS line,
        # not the AS clause on the line after it.
        return f"({base})\nAS {c['target']}"

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
