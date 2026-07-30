#!/usr/bin/env python3
"""
verify_silver_layer.py -- the gate that makes data-modeling gold-layer-only. Before this skill
proposes a single fact or dimension, every silver-layer object it would source from must pass
this check, or the run refuses to design against it (see SKILL.md step 1 and
contracts/model-spec.schema.json's silver_verification object).

This checks CURATION-LAYER STRUCTURE, not data quality. A table can fail every check data-quality
would run (nulls, orphaned FKs, duplicated natural keys) and still pass THIS check, because those
are quality problems a curated layer can still have -- see references/silver-verification.md for
why that distinction matters and isn't hair-splitting. Conversely a table with perfectly clean
data but no declared primary key, raw-ingestion columns, and PascalCase source-system naming is
NOT curated by this check's definition, no matter how clean its values are.

Five deterministic, measured signals per object (no judgment calls):
  1. primary_key_declared     -- get_constraints().primary_key is non-empty.
  2. primary_key_profiled_unique -- the declared PK, if any, is ACTUALLY unique when profiled
     (Unity Catalog PK/FK constraints are informational, not enforced -- a declared PK that isn't
     really unique is exactly the gap this signal exists to catch. Skipped/false if no PK declared.)
  3. table_comment_present    -- get_table_comment() is non-null and non-empty.
  4. no_raw_ingestion_artifact_columns -- no column name matches the raw-ingestion-artifact
     denylist pattern (_rescued_data, _ingest*, _load*, _raw*, _file*, and similar).
  5. business_meaningful_naming_ratio >= 0.8 -- at least 80% of columns are snake_case
     (business-conformed naming), not PascalCase/camelCase source-system field codes.

verified = True only when ALL FIVE signals pass. layer_detected is bronze_or_raw when a PK is
undeclared AND raw-ingestion columns are present; silver_curated when verified; silver_uncurated
when a PK is declared but one or more other signals fail; unknown otherwise. Passing --schema gold
short-circuits to layer_detected "gold" / verified True (an existing gold object is, by
definition, at least as curated as what this skill would produce) without running the five checks.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lakehouse_adapter import LakehouseAdapter, SQLiteFixtureAdapter  # noqa: E402

RAW_INGESTION_ARTIFACT_PATTERN = re.compile(
    r"^_?(rescued_data|ingest|load|raw|file|batch)[a-z_]*$", re.IGNORECASE
)
SNAKE_CASE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def verify_object(adapter: LakehouseAdapter, schema: str, table: str) -> dict:
    if schema == "gold":
        return {
            "object": f"{schema}.{table}", "layer_detected": "gold", "verified": True,
            "signals": {}, "evidence": f"{schema}.{table} is already in the gold schema -- "
                                        "treated as at least as curated as this skill's own output.",
            "reason_if_not_verified": None,
        }

    columns = adapter.get_columns(schema, table)
    constraints = adapter.get_constraints(schema, table)
    table_comment = adapter.get_table_comment(schema, table)
    column_names = [c.name for c in columns]

    pk_declared = bool(constraints.primary_key)
    pk_profiled_unique = None
    if pk_declared:
        u = adapter.check_uniqueness(schema, table, constraints.primary_key)
        pk_profiled_unique = u["is_unique"]

    comment_present = bool(table_comment and table_comment.strip())

    raw_artifact_cols = [c for c in column_names if RAW_INGESTION_ARTIFACT_PATTERN.match(c)]
    no_raw_artifacts = len(raw_artifact_cols) == 0

    snake_case_cols = [c for c in column_names if SNAKE_CASE_PATTERN.match(c)]
    naming_ratio = (len(snake_case_cols) / len(column_names)) if column_names else 0.0
    naming_ok = naming_ratio >= 0.8

    signals = {
        "primary_key_declared": pk_declared,
        "primary_key_profiled_unique": pk_profiled_unique,
        "table_comment_present": comment_present,
        "no_raw_ingestion_artifact_columns": no_raw_artifacts,
        "business_meaningful_naming_ratio": round(naming_ratio, 3),
    }

    verified = bool(
        pk_declared and (pk_profiled_unique is True) and comment_present
        and no_raw_artifacts and naming_ok
    )

    if not pk_declared and not no_raw_artifacts:
        layer_detected = "bronze_or_raw"
    elif verified:
        layer_detected = "silver_curated"
    elif pk_declared:
        layer_detected = "silver_uncurated"
    else:
        layer_detected = "unknown"

    evidence_parts = [
        f"primary key {'declared (' + ', '.join(constraints.primary_key) + ')' if pk_declared else 'NOT declared'}",
        f"declared PK profiled {'unique' if pk_profiled_unique else 'NOT unique'}" if pk_declared else "no PK to profile",
        f"table comment {'present' if comment_present else 'absent'}",
        f"raw-ingestion-artifact columns: {raw_artifact_cols or 'none'}",
        f"business-meaningful (snake_case) naming: {naming_ratio:.0%} of columns ({snake_case_cols})"
        if naming_ratio < 1.0 else "business-meaningful (snake_case) naming: 100% of columns",
    ]
    evidence = f"{schema}.{table}: " + "; ".join(evidence_parts) + "."

    reason_if_not_verified = None
    if not verified:
        failed = [k for k, v in signals.items() if v is False or v == 0.0 or (isinstance(v, float) and v < 0.8)]
        reason_if_not_verified = (
            f"{schema}.{table} failed curation signal(s): {', '.join(failed) if failed else 'see evidence'}. "
            f"{evidence}"
        )

    return {
        "object": f"{schema}.{table}", "layer_detected": layer_detected, "verified": verified,
        "signals": signals, "evidence": evidence, "reason_if_not_verified": reason_if_not_verified,
    }


def verify_silver_layer(adapter: LakehouseAdapter, objects: list[tuple]) -> dict:
    """objects: list of (schema, table). Returns an aggregate verdict plus per-object detail --
    data-modeling refuses to design against ANY unverified source, so the aggregate is an AND
    over every object referenced, not a majority vote."""
    results = [verify_object(adapter, schema, table) for schema, table in objects]
    all_verified = all(r["verified"] for r in results)
    combined_evidence = " | ".join(r["evidence"] for r in results)
    layer_detected = results[0]["layer_detected"] if len(results) == 1 else (
        "silver_curated" if all_verified else "unknown"
    )
    failing = [r for r in results if not r["verified"]]
    reason_if_not_verified = None
    if failing:
        reason_if_not_verified = " | ".join(r["reason_if_not_verified"] for r in failing)
    return {
        "verified": all_verified,
        "layer_detected": layer_detected,
        "evidence": combined_evidence,
        "reason_if_not_verified": reason_if_not_verified,
        "per_object": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lakehouse-dir", required=True)
    parser.add_argument("--catalog", default="acme_retail_dev")
    parser.add_argument("--object", action="append", required=True,
                         help="schema.table, repeatable -- every source object the proposed model would read from")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    adapter = SQLiteFixtureAdapter(args.lakehouse_dir, catalog=args.catalog)
    objects = []
    for spec in args.object:
        schema, table = spec.split(".", 1)
        objects.append((schema, table))

    result = verify_silver_layer(adapter, objects)
    output = json.dumps(result, indent=2, default=str)
    if args.out:
        args.out.write_text(output)
        print(f"Silver verification written to {args.out} -- verified={result['verified']}")
    else:
        print(output)
    sys.exit(0 if result["verified"] else 1)


if __name__ == "__main__":
    main()
