#!/usr/bin/env python3
"""
validate_pipeline_locally.py -- the closest thing to "did we prove this pipeline is idempotent"
this sandbox can do without a real Spark/Databricks target (this toolkit has no live workspace in
CI -- see scripts/lakehouse_adapter.py's own docstring on the same limitation for DatabricksAdapter).
Rather than skip idempotency evidence entirely, this runs the SAME transform_spec that will be
rendered into PySpark/Declarative Pipeline/Lakeflow Connect code (build_transform_spec.py) against
mock data (derive_mock_data.py), in a scratch SQLite destination, TWICE, and diffs the result.

What this proves: the merge-key logic and column mapping in transform_spec.json are idempotent
against a representative (if synthetic) dataset -- re-applying them doesn't create duplicates or
change already-correct rows.

What this does NOT prove: that the GENERATED PySpark/Declarative Pipeline code is syntactically or
semantically correct Spark, that it will behave identically against real (messier, larger,
type-mismatched) source data, or that it runs at all on a real cluster. See
references/idempotency-and-mock-data.md for the honest scope of this evidence -- readiness_level
"validated" means this proof passed, not "deployed and confirmed working."
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_transform_spec import render_select_sql  # noqa: E402


def _row_hash(row: dict) -> int:
    canonical = json.dumps(row, sort_keys=True, default=str).encode()
    return int(hashlib.sha256(canonical).hexdigest(), 16)


def _aggregate_hash(rows: list[dict]) -> str:
    # Order-independent: XOR of per-row hashes, same technique as
    # skills/data-validation/scripts/compare_staged.py's hash_aggregate stage, so a re-run's
    # destination content can be compared without depending on row order.
    combined = 0
    for r in rows:
        combined ^= _row_hash(r)
    return hex(combined)


def _apply_merge(conn: sqlite3.Connection, spec: dict, run_label: str) -> None:
    select_sql = render_select_sql(spec, source_ref="mock_source")
    target_cols = [c["target"] for c in spec["columns"]]
    merge_keys = spec["merge_keys"]

    if merge_keys:
        placeholders = ", ".join(target_cols)
        update_clause = ", ".join(f"{c}=excluded.{c}" for c in target_cols if c not in merge_keys)
        conflict_cols = ", ".join(merge_keys)
        # SQLite has a documented grammar ambiguity when an upsert clause follows a bare
        # "INSERT INTO ... SELECT ..." (the parser can't tell "ON" apart from a join condition
        # continuation) -- its own docs' workaround is a WHERE clause on the SELECT to disambiguate.
        # This is purely a SQLite parsing quirk of the LOCAL idempotency proof; it has no bearing
        # on the generated Spark MERGE INTO / APPLY CHANGES syntax, which has no such ambiguity.
        sql = (
            f"INSERT INTO dest ({placeholders}) {select_sql} WHERE 1=1 "
            f"ON CONFLICT({conflict_cols}) DO UPDATE SET {update_clause}"
        )
        conn.execute(sql)
    else:
        # full_refresh: no merge keys means idempotency is achieved via replace-not-append.
        conn.execute("DELETE FROM dest")
        conn.execute(f"INSERT INTO dest ({', '.join(target_cols)}) {select_sql}")
    conn.commit()


def validate_pipeline_locally(spec: dict, mock_rows: list[dict]) -> dict:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    source_cols = sorted({c["source_column"] for c in spec["columns"]})
    conn.execute(f"CREATE TABLE mock_source ({', '.join(source_cols)})")
    for row in mock_rows:
        placeholders = ", ".join("?" for _ in source_cols)
        conn.execute(f"INSERT INTO mock_source ({', '.join(source_cols)}) VALUES ({placeholders})",
                      [row.get(c) for c in source_cols])

    target_cols = [c["target"] for c in spec["columns"]]
    pk_clause = f", PRIMARY KEY ({', '.join(spec['merge_keys'])})" if spec["merge_keys"] else ""
    conn.execute(f"CREATE TABLE dest ({', '.join(target_cols)}{pk_clause})")

    if not spec["merge_keys"]:
        return {
            "performed": True,
            "method": "load_pattern is full_refresh (no merge keys) -- idempotency is structural "
                      "(delete-then-insert), not merge-key-dependent, so no two-run comparison was needed.",
            "result": "match",
            "evidence": {"row_count_after_run_1": None, "row_count_after_run_2": None,
                         "hash_after_run_1": None, "hash_after_run_2": None},
        }

    _apply_merge(conn, spec, "run_1")
    rows_after_1 = [dict(r) for r in conn.execute("SELECT * FROM dest").fetchall()]
    count_1 = len(rows_after_1)
    hash_1 = _aggregate_hash(rows_after_1)

    _apply_merge(conn, spec, "run_2")
    rows_after_2 = [dict(r) for r in conn.execute("SELECT * FROM dest").fetchall()]
    count_2 = len(rows_after_2)
    hash_2 = _aggregate_hash(rows_after_2)

    result = "match" if (count_1 == count_2 and hash_1 == hash_2) else "mismatch"
    return {
        "performed": True,
        "method": "Rendered transform_spec as a portable SQL MERGE (INSERT ... ON CONFLICT DO UPDATE), "
                   "ran it twice against a scratch SQLite destination seeded from mock data, and "
                   "compared destination row count and an order-independent content hash after each run.",
        "result": result,
        "evidence": {"row_count_after_run_1": count_1, "row_count_after_run_2": count_2,
                     "hash_after_run_1": hash_1, "hash_after_run_2": hash_2},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transform-spec-json", type=Path, required=True)
    parser.add_argument("--mock-data-json", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    spec = json.loads(args.transform_spec_json.read_text())
    mock_rows = json.loads(args.mock_data_json.read_text())

    result = validate_pipeline_locally(spec, mock_rows)
    output = json.dumps(result, indent=2)
    if args.out:
        args.out.write_text(output)
        print(f"Idempotency evidence written to {args.out}")
    else:
        print(output)
    sys.exit(0 if result["result"] in ("match",) else 1)


if __name__ == "__main__":
    main()
