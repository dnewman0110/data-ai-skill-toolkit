#!/usr/bin/env python3
"""
compare_staged.py -- the deterministic staged comparison at the heart of data-validation.
row_count -> hash_aggregate -> column_aggregate -> row_level_diff, digging deeper only when the
prior stage found a discrepancy. See references/staged-comparison.md for the full design
rationale; this docstring covers only what a reader needs to trust the mechanics.

Stopping criteria (the "digging deeper only as needed" the toolkit spec calls for):
  - row_count is always computed (a single COUNT(*) per side, pushed down -- cheap regardless of
    table size) and is informational; it never stops the comparison on its own, because matching
    counts do not prove matching content.
  - hash_aggregate is the first REAL stopping point: an order-independent aggregate over every
    row's normalized content-hash, per side. If the aggregates match, the comparison stops here --
    column_aggregate and row_level_diff are recorded as not executed. This is the single query
    (well, single bounded fetch) that answers "do these match" for the common case where they do.
  - column_aggregate and row_level_diff only run when hash_aggregate found a mismatch, and are
    computed from the SAME fetched-and-normalized rows hash_aggregate already pulled (no second
    fetch) -- column_aggregate for per-column triage, row_level_diff for the specific differing
    keys, capped at row_level_diff_row_cap for how many get full detail in the report.

Scale caveat, stated once here rather than hidden: hash_aggregate/column_aggregate/row_level_diff
all require fetching every row on both sides up to `content_check_row_cap` (bounded, and gated by
the pre-flight cost estimate before this even runs -- see build_validation_findings.py). Above that
cap, this module reports row_count only and marks the deeper stages as skipped with a stated
reason, rather than silently truncating to a sample and calling it complete. A genuinely
production-scale implementation (multi-million-row tables) would push hash/aggregate computation
down via SQL on each side instead of fetching to Python -- see references/staged-comparison.md's
"Known scaling limit" section for what that would take and why it isn't built here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize import infer_column_treatment, normalize_row, row_hash  # noqa: E402


def _aggregate_hash(row_hashes: list[str]) -> str:
    """Order-independent combination of per-row hashes: XOR of each hash's integer value. Two
    sides with the exact same set of (row) hashes produce the same aggregate regardless of fetch
    order; a changed, added, or removed row changes the aggregate (collision risk is the same as
    sha256's, i.e. negligible)."""
    acc = 0
    for h in row_hashes:
        acc ^= int(h, 16)
    return format(acc, "x")


def compare(source_adapter, source_schema: str, source_table: str,
            target_adapter, target_schema: str, target_table: str,
            key_columns: list[str], compare_columns: list[str] | None = None,
            content_check_row_cap: int = 100_000, row_level_diff_row_cap: int = 5000,
            known_acceptable_differences: list[dict] | None = None) -> dict:
    known_acceptable_differences = known_acceptable_differences or []
    stages = []

    # -- Stage 1: row_count --
    source_count = source_adapter.row_count(source_schema, source_table, exact=True)
    target_count = target_adapter.row_count(target_schema, target_table, exact=True)
    stages.append({
        "stage": "row_count", "executed": True, "stopped_here": False,
        "result": {"source_count": source_count, "target_count": target_count},
    })

    # -- Column selection: intersect unless caller specified an explicit compare_columns --
    source_cols = {c.name: c.type for c in source_adapter.get_columns(source_schema, source_table)}
    target_cols = {c.name: c.type for c in target_adapter.get_columns(target_schema, target_table)}
    if compare_columns is None:
        compare_columns = sorted(set(source_cols) & set(target_cols))
    for k in key_columns:
        if k not in compare_columns:
            compare_columns.append(k)

    # -- Content-check scale gate --
    if source_count > content_check_row_cap or target_count > content_check_row_cap:
        stages.append({
            "stage": "hash_aggregate", "executed": False, "stopped_here": False,
            "result": {"skipped_reason": f"source/target row count exceeds content_check_row_cap "
                                          f"({content_check_row_cap}); only row_count was compared. "
                                          f"See references/staged-comparison.md 'Known scaling limit'."},
        })
        stages.append({"stage": "column_aggregate", "executed": False, "stopped_here": False, "result": {}})
        stages.append({"stage": "row_level_diff", "executed": False, "stopped_here": False, "result": {},
                        "row_cap": row_level_diff_row_cap})
        return {
            "stages": stages, "discrepancies": [], "known_acceptable_differences_excluded": [],
            "summary": {"deepest_stage_reached": "row_count", "match": source_count == target_count},
            "normalization_applied": {
                "ordering": "Rows matched by declared/candidate key, not fetch order (content comparison skipped at this scale).",
                "nulls": "N/A -- content comparison skipped.",
                "floats": "N/A -- content comparison skipped.",
                "timezones": "N/A -- content comparison skipped.",
            },
        }

    # -- Fetch + normalize both sides once, reused by stages 2-4 --
    source_rows = source_adapter.fetch_rows(source_schema, source_table, compare_columns,
                                             order_by=key_columns, limit=content_check_row_cap)
    target_rows = target_adapter.fetch_rows(target_schema, target_table, compare_columns,
                                             order_by=key_columns, limit=content_check_row_cap)

    column_treatments = {}
    for col in compare_columns:
        sample_vals = [r.get(col) for r in (source_rows[:20] + target_rows[:20])]
        column_treatments[col] = infer_column_treatment(source_cols.get(col), target_cols.get(col), sample_vals)

    def keyed(rows):
        out = {}
        for r in rows:
            key = tuple(r[k] for k in key_columns)
            normalized = normalize_row(r, column_treatments)
            out[key] = {"raw": r, "normalized": normalized, "hash": row_hash(normalized, compare_columns)}
        return out

    source_keyed = keyed(source_rows)
    target_keyed = keyed(target_rows)

    # -- Stage 2: hash_aggregate --
    source_agg = _aggregate_hash([v["hash"] for v in source_keyed.values()])
    target_agg = _aggregate_hash([v["hash"] for v in target_keyed.values()])
    hash_match = source_agg == target_agg
    stages.append({
        "stage": "hash_aggregate", "executed": True, "stopped_here": hash_match,
        "result": {"source_aggregate": source_agg, "target_aggregate": target_agg, "match": hash_match},
    })

    normalization_applied = {
        "ordering": "Rows matched by declared/candidate key (" + ",".join(key_columns) + "), not fetch order.",
        "nulls": "NULL normalized to a fixed sentinel distinct from any real value before hashing.",
        "floats": f"Numeric-typed or numeric-looking columns rounded to {4} decimal places before hashing.",
        "timezones": "Columns whose values parse as timestamps normalized to UTC ISO-8601 before hashing.",
    }

    if hash_match:
        stages.append({"stage": "column_aggregate", "executed": False, "stopped_here": False, "result": {}})
        stages.append({"stage": "row_level_diff", "executed": False, "stopped_here": False, "result": {},
                        "row_cap": row_level_diff_row_cap})
        return {
            "stages": stages, "discrepancies": [], "known_acceptable_differences_excluded": [],
            "summary": {"deepest_stage_reached": "hash_aggregate", "match": True},
            "normalization_applied": normalization_applied,
        }

    # -- Stage 3: column_aggregate (triage) --
    column_aggregate_result = {}
    for col in compare_columns:
        s_vals = [v["normalized"][col] for v in source_keyed.values()]
        t_vals = [v["normalized"][col] for v in target_keyed.values()]
        s_numeric = [v for v in s_vals if isinstance(v, (int, float))]
        t_numeric = [v for v in t_vals if isinstance(v, (int, float))]
        col_result = {
            "source_count": len(s_vals), "target_count": len(t_vals),
            "source_null_count": sum(1 for v in s_vals if v == " __NULL__ "),
            "target_null_count": sum(1 for v in t_vals if v == " __NULL__ "),
        }
        if s_numeric and t_numeric:
            col_result["source_sum"] = round(sum(s_numeric), 4)
            col_result["target_sum"] = round(sum(t_numeric), 4)
        col_result["differs"] = (
            col_result["source_count"] != col_result["target_count"]
            or col_result["source_null_count"] != col_result["target_null_count"]
            or col_result.get("source_sum") != col_result.get("target_sum")
        )
        column_aggregate_result[col] = col_result
    stages.append({
        "stage": "column_aggregate", "executed": True, "stopped_here": False,
        "result": {"columns": column_aggregate_result,
                   "columns_with_aggregate_mismatch": sorted(c for c, r in column_aggregate_result.items() if r["differs"])},
    })

    # -- Stage 4: row_level_diff --
    source_keys, target_keys = set(source_keyed), set(target_keyed)
    missing_from_target = sorted(source_keys - target_keys)
    extra_in_target = sorted(target_keys - source_keys)
    changed = sorted(k for k in (source_keys & target_keys) if source_keyed[k]["hash"] != target_keyed[k]["hash"])

    discrepancies = []
    budget = row_level_diff_row_cap

    def key_dict(key):
        return dict(zip(key_columns, key))

    for key in missing_from_target:
        if budget <= 0:
            break
        discrepancies.append({
            "kind": "missing_from_target", "key": key_dict(key),
            "columns_affected": [], "source_row": source_keyed[key]["raw"], "target_row": None,
        })
        budget -= 1
    for key in extra_in_target:
        if budget <= 0:
            break
        discrepancies.append({
            "kind": "extra_in_target", "key": key_dict(key),
            "columns_affected": [], "source_row": None, "target_row": target_keyed[key]["raw"],
        })
        budget -= 1
    for key in changed:
        if budget <= 0:
            break
        s_norm, t_norm = source_keyed[key]["normalized"], target_keyed[key]["normalized"]
        affected = [c for c in compare_columns if s_norm.get(c) != t_norm.get(c)]
        discrepancies.append({
            "kind": "changed", "key": key_dict(key), "columns_affected": affected,
            "source_row": source_keyed[key]["raw"], "target_row": target_keyed[key]["raw"],
        })
        budget -= 1

    stages.append({
        "stage": "row_level_diff", "executed": True, "stopped_here": True,
        "result": {"rows_compared": len(source_keys | target_keys),
                   "missing_from_target_count": len(missing_from_target),
                   "extra_in_target_count": len(extra_in_target),
                   "changed_count": len(changed),
                   "rows_returned_with_full_detail": len(discrepancies)},
        "row_cap": row_level_diff_row_cap,
    })

    # -- Apply known_acceptable_differences exclusions --
    excluded = []
    kept = []
    for d in discrepancies:
        matched_rule = None
        for rule in known_acceptable_differences:
            if rule.get("type") == "column_ignore" and rule.get("column") in d.get("columns_affected", []):
                matched_rule = rule
                break
            rule_key = rule.get("key")
            if isinstance(rule_key, (list, tuple)):
                rule_key = dict(zip(key_columns, rule_key))
            if rule.get("type") == "key_ignore" and rule_key == d.get("key"):
                matched_rule = rule
                break
        if matched_rule:
            excluded.append({"description": matched_rule.get("description", ""),
                              "declared_by": matched_rule.get("declared_by", "toolkit.yaml"),
                              "rule": f"{matched_rule.get('type')}:{matched_rule.get('column') or matched_rule.get('key')}"})
        else:
            kept.append(d)

    return {
        "stages": stages, "discrepancies": kept, "known_acceptable_differences_excluded": excluded,
        "summary": {"deepest_stage_reached": "row_level_diff", "match": len(kept) == 0},
        "normalization_applied": normalization_applied,
    }
