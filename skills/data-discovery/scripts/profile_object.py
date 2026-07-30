#!/usr/bin/env python3
"""
profile_object.py -- deterministic profiling of a single source object (table), bounded by a
sample size, producing everything discovery needs to determine grain and propose tests WITHOUT
any interpretation. Pure measurement: constraints as declared, null rates, distinct counts,
candidate-key uniqueness, declared-FK orphan rates. No naming heuristics live here -- "does
customer_id look like it references customers.customer_id" is a judgment call the agent makes
per SKILL.md, not something this script guesses at. This script only checks relationships it's
explicitly told to check (declared FKs automatically; anything else via --candidate-fk).

Bounding: every profiling operation respects --sample-size (default from toolkit.yaml's
sample_data.default_sample_size). Row count and byte estimate are always computed against the
full object (they're cheap -- see lakehouse_adapter.py) so grain/uniqueness confidence can note
whether a check covered the full population or a sample; per confidence-rubric.md, full-population
checks earn higher confidence than sampled ones in the same evidence-class band.

Output: one JSON object per profiled table, on stdout by default or written to --out.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lakehouse_adapter import LakehouseAdapter, SQLiteFixtureAdapter  # noqa: E402


def profile_table(adapter: LakehouseAdapter, schema: str, table: str, sample_size: int | None,
                   candidate_fks: list[dict] | None = None) -> dict:
    columns = adapter.get_columns(schema, table)
    constraints = adapter.get_constraints(schema, table)
    table_comment = adapter.get_table_comment(schema, table)
    total_rows = adapter.row_count(schema, table, exact=True)

    numeric_types = {"integer", "int", "bigint", "smallint", "real", "double", "float",
                      "decimal", "numeric"}

    column_profiles = []
    for col in columns:
        p = adapter.profile_column(schema, table, col.name, sample_size=sample_size)
        is_declared_numeric = any(t in col.type.lower() for t in numeric_types)
        looks_numeric = None
        if not is_declared_numeric:
            # Deterministic type-mismatch signal: for TEXT/VARCHAR-declared columns, sample raw
            # values and check whether they all parse as numbers. This is a measured fact (either
            # every sampled non-null value parses as a float, or it doesn't) -- not a guess about
            # what the column "should" be. Flags exactly the total_amt-stored-as-text kind of
            # source/target type mismatch without any LLM involved.
            sample_vals = adapter.sample_rows(schema, table, [col.name], min(50, max(sample_size or 50, 1)))
            non_null_vals = [r[col.name] for r in sample_vals if r[col.name] is not None]
            if non_null_vals:
                parseable = 0
                for v in non_null_vals:
                    try:
                        float(v)
                        parseable += 1
                    except (TypeError, ValueError):
                        pass
                looks_numeric = (parseable == len(non_null_vals))
        column_profiles.append({
            "name": col.name,
            "declared_type": col.type,
            "declared_nullable": col.nullable,
            "comment": col.comment,
            "total_rows": p.total_rows,
            "sampled_rows": p.sampled_rows,
            "sample_covers_full_population": p.sampled_rows >= p.total_rows,
            "null_count": p.null_count,
            "null_rate": (p.null_count / p.sampled_rows) if p.sampled_rows else 0.0,
            "distinct_count": p.distinct_count,
            "min_value": p.min_value,
            "max_value": p.max_value,
            "is_declared_numeric": is_declared_numeric,
            "looks_numeric_but_declared_text": looks_numeric is True and not is_declared_numeric,
        })

    candidate_keys = []
    key_candidates_to_check = []
    if constraints.primary_key:
        key_candidates_to_check.append(("declared_primary_key", constraints.primary_key))
    else:
        # No declared PK -- try the single leading column and, if the table looks like a line-item
        # fact (has a second integer-ish column right after it), the pair. This is a bounded,
        # deterministic heuristic about WHICH columns to test for uniqueness -- profiling still
        # decides the answer; it is not proposing a mapping or making a business judgment.
        if columns:
            key_candidates_to_check.append(("first_column_only", [columns[0].name]))
            if len(columns) > 1:
                key_candidates_to_check.append(("first_two_columns", [columns[0].name, columns[1].name]))

    # In addition to the primary/positional candidate above, always check columns that are
    # shaped like a natural/business key by naming convention, regardless of whether a surrogate
    # PK is declared. This matters: a table can have a perfectly unique surrogate key while a
    # natural key it also carries (e.g. an upstream CRM's business identifier) has silently
    # become duplicated -- a real data-quality problem the surrogate-key check alone would never
    # surface. Still a bounded, deterministic heuristic about WHICH columns to check, same as
    # above -- the uniqueness answer itself is always measured, never guessed.
    import re as _re
    natural_key_pattern = _re.compile(r"(_number|_code|_key|email)$", _re.IGNORECASE)
    already_checked = {tuple(c) for _, c in key_candidates_to_check}
    pk_member_columns = set(constraints.primary_key)
    for col in columns:
        if col.name in pk_member_columns:
            # Already covered by the composite/declared PK check above; testing one member
            # column alone (e.g. line_number out of (order_id, line_number)) isn't a real
            # candidate-key question and only produces a misleading "not unique" finding.
            continue
        if natural_key_pattern.search(col.name) and (col.name,) not in already_checked:
            key_candidates_to_check.append(("natural_key_naming_heuristic", [col.name]))

    for source, cols in key_candidates_to_check:
        result = adapter.check_uniqueness(schema, table, cols, sample_size=sample_size)
        candidate_keys.append({
            "columns": cols, "source": source,
            "rows_checked": result["rows_checked"], "rows_with_null_key": result["rows_with_null_key"],
            "distinct_count": result["distinct_count"],
            "is_unique": result["is_unique"], "duplicate_count": result["duplicate_count"],
            "checked_full_population": result["rows_checked"] >= total_rows,
        })

    fk_checks = []
    for fk in constraints.foreign_keys:
        col = fk["columns"][0]
        result = adapter.count_orphans(schema, table, col, fk["ref_schema"], fk["ref_table"],
                                        fk["ref_columns"][0], sample_size=sample_size)
        fk_checks.append({
            "column": col, "declared": True,
            "ref_object": f"{fk['ref_schema']}.{fk['ref_table']}", "ref_column": fk["ref_columns"][0],
            **result,
        })
    for cfk in (candidate_fks or []):
        result = adapter.count_orphans(schema, table, cfk["column"], cfk["ref_schema"], cfk["ref_table"],
                                        cfk["ref_column"], sample_size=sample_size)
        fk_checks.append({
            "column": cfk["column"], "declared": False,
            "ref_object": f"{cfk['ref_schema']}.{cfk['ref_table']}", "ref_column": cfk["ref_column"],
            **result,
        })

    return {
        "object": f"{schema}.{table}",
        "row_count": total_rows,
        "estimated_bytes": adapter.estimate_bytes(schema, table),
        "table_comment": table_comment,
        "declared_primary_key": constraints.primary_key,
        "declared_not_null": constraints.not_null,
        "columns": column_profiles,
        "candidate_keys": candidate_keys,
        "fk_checks": fk_checks,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lakehouse-dir", required=True)
    parser.add_argument("--catalog", default="acme_retail_dev")
    parser.add_argument("--schema", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--sample-size", type=int, default=None)
    parser.add_argument("--candidate-fk", action="append", default=[],
                         help="column:ref_schema.ref_table.ref_column, repeatable")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    candidate_fks = []
    for spec in args.candidate_fk:
        col, ref = spec.split(":", 1)
        ref_schema, ref_table, ref_column = ref.split(".")
        candidate_fks.append({"column": col, "ref_schema": ref_schema, "ref_table": ref_table, "ref_column": ref_column})

    adapter = SQLiteFixtureAdapter(args.lakehouse_dir, catalog=args.catalog)
    result = profile_table(adapter, args.schema, args.table, args.sample_size, candidate_fks)

    output = json.dumps(result, indent=2, default=str)
    if args.out:
        args.out.write_text(output)
    else:
        print(output)


if __name__ == "__main__":
    main()
