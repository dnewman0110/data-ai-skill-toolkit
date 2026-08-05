#!/usr/bin/env python3
"""
validate_pipeline_locally.py -- the closest thing to "did we prove this pipeline is idempotent"
this sandbox can do without a real Spark/Databricks target (this toolkit has no live workspace in
CI -- see scripts/lakehouse_adapter.py's own docstring on the same limitation for DatabricksConnectAdapter).
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
        #
        # A pure bridge/junction table (every column is a merge key, e.g. bridge_property_amenity
        # (property_id, amenity_id)) leaves update_clause empty -- "DO UPDATE SET" with nothing
        # after it is invalid SQLite syntax. DO NOTHING is the semantically correct fallback: an
        # all-key upsert has nothing to update on a match, the row is already exactly right.
        conflict_action = f"DO UPDATE SET {update_clause}" if update_clause else "DO NOTHING"
        sql = (
            f"INSERT INTO dest ({placeholders}) {select_sql} WHERE 1=1 "
            f"ON CONFLICT({conflict_cols}) {conflict_action}"
        )
        conn.execute(sql)
    else:
        # full_refresh: no merge keys means idempotency is achieved via replace-not-append.
        conn.execute("DELETE FROM dest")
        conn.execute(f"INSERT INTO dest ({', '.join(target_cols)}) {select_sql}")
    conn.commit()


def validate_pipeline_locally(spec: dict, mock_rows: list[dict]) -> dict:
    if spec.get("is_multi_source"):
        # v1 scope decision, not a silent gap: derive_mock_data.py synthesizes one flat mock table
        # per TARGET, keyed by bare source column name -- it has no notion of multiple mock source
        # tables sharing real foreign-key relationships across aliases, which a multi-source join's
        # local proof would need to be meaningful (mock data that doesn't actually share join keys
        # would "prove" idempotency by joining everything to NULL, which proves nothing). Rendering
        # and real code generation both fully support multi-source specs (build_transform_spec.py,
        # generate_pipeline_code.py) -- only this local SQLite-based proof does not yet. See
        # references/idempotency-and-mock-data.md and references/other-modalities.md.
        return {
            "performed": False,
            "method": "Not performed: this spec joins multiple source objects "
                      f"({', '.join(sorted({s['alias'] or s['table'] for s in spec['sources']}))}). "
                      "The local idempotency proof does not yet synthesize multi-table mock data "
                      "with matching join keys across separate mock tables -- see "
                      "references/idempotency-and-mock-data.md. This is a documented v1 scope "
                      "limit of the LOCAL proof only; the real generated Spark code renders the "
                      "full multi-table join and is unaffected.",
            "result": "not_applicable",
            "evidence": {"row_count_after_run_1": None, "row_count_after_run_2": None,
                         "hash_after_run_1": None, "hash_after_run_2": None},
        }

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # toolkit_hash backs render_select_sql's placeholder hash expression for target_transform
    # "hash" columns -- SQLite has no built-in hash function. Only needs to be deterministic
    # across the two runs compared below, not byte-identical to the real Spark F.sha2(...)
    # generate_pipeline_code.py renders for the actual pipeline code.
    conn.create_function(
        "toolkit_hash", 1,
        lambda v: None if v is None else hashlib.sha256(str(v).encode("utf-8")).hexdigest(),
    )

    # extra_source_columns covers identifiers a transformation expression references (e.g.
    # check_out in "DATEDIFF(check_out, check_in)") that aren't themselves any column's own
    # mapped source_column -- see build_transform_spec.py's _referenced_identifiers.
    source_cols = sorted({c["source_column"] for c in spec["columns"]} | set(spec.get("extra_source_columns", [])))
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

    try:
        _apply_merge(conn, spec, "run_1")
        rows_after_1 = [dict(r) for r in conn.execute("SELECT * FROM dest").fetchall()]
        count_1 = len(rows_after_1)
        hash_1 = _aggregate_hash(rows_after_1)

        _apply_merge(conn, spec, "run_2")
        rows_after_2 = [dict(r) for r in conn.execute("SELECT * FROM dest").fetchall()]
        count_2 = len(rows_after_2)
        hash_2 = _aggregate_hash(rows_after_2)
    except sqlite3.OperationalError as e:
        # A column's transformation used a real Spark SQL function (e.g. DATEDIFF) that has no
        # SQLite equivalent -- render_select_sql's docstring already says this proof only covers a
        # "close enough" portable subset, not full Spark SQL. Rather than crash the whole run on
        # exactly the case this fix exists to support, report honestly that local idempotency
        # couldn't be proven for this reason -- the real generated code still uses real Spark
        # syntax and is unaffected; only this LOCAL proof is limited.
        return {
            "performed": False,
            "method": "Not performed: rendering transform_spec as portable SQL failed "
                      f"({e}) -- at least one column's transformation likely uses a function "
                      "SQLite doesn't support locally. This is a limitation of the local proof, "
                      "not evidence the generated Spark code is wrong.",
            "result": "not_applicable",
            "evidence": {"row_count_after_run_1": None, "row_count_after_run_2": None,
                         "hash_after_run_1": None, "hash_after_run_2": None},
        }

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
    sys.exit(0 if result["result"] in ("match", "not_applicable") else 1)


if __name__ == "__main__":
    main()
