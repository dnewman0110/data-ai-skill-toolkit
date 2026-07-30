#!/usr/bin/env python3
"""
run_checks.py -- the deterministic scan at the heart of data-quality. Executes a list of check
definitions (hand-authored, contract-derived, or a merge of both -- see
derive_checks_from_contract.py) against a single target object and returns, for each, a status
(passed/failed/warned/not_evaluated), the measured value, and the threshold that was in effect.
No LLM, no judgment: same checks against unchanged data always produce the same result.

Status is computed the same way for every check type:
    - Can't execute at all (column/ref object missing, params malformed, custom_sql errors) ->
      not_evaluated, with reason_not_evaluated explaining exactly why.
    - Executes and meets its threshold -> passed.
    - Executes and violates its threshold -> failed (if severity is blocking) or warned (if
      severity is warning). severity is a configured property of the check ("how much this
      matters"); status is the measured outcome -- keeping them separate means a warning-severity
      check that fails still shows up as "warned" rather than silently looking identical to a
      passed check or to a blocking failure.
    - No threshold configured for a check type that needs one (e.g. row_count with neither
      min_rows nor max_rows set) -> not_evaluated, not a silent always-pass.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lakehouse_adapter import LakehouseAdapter, SQLiteFixtureAdapter  # noqa: E402


def _status(violated: bool, severity: str) -> str:
    if not violated:
        return "passed"
    return "failed" if severity == "blocking" else "warned"


def _run_row_count(adapter, schema, table, check):
    params = check.get("params", {})
    count = adapter.row_count(schema, table, exact=True)
    min_rows, max_rows = params.get("min_rows"), params.get("max_rows")
    if min_rows is None and max_rows is None:
        return {"status": "not_evaluated", "measured_value": count, "threshold": None,
                "reason_not_evaluated": "row_count check has neither min_rows nor max_rows configured."}
    violated = (min_rows is not None and count < min_rows) or (max_rows is not None and count > max_rows)
    return {"status": _status(violated, check["severity"]), "measured_value": count,
            "threshold": {"min_rows": min_rows, "max_rows": max_rows}, "reason_not_evaluated": None}


def _run_null_rate(adapter, schema, table, check):
    column = check["column"]
    params = check.get("params", {})
    max_null_rate = params.get("max_null_rate")
    if max_null_rate is None:
        return {"status": "not_evaluated", "measured_value": None, "threshold": None,
                "reason_not_evaluated": "null_rate check has no max_null_rate configured."}
    try:
        profile = adapter.profile_column(schema, table, column)
    except Exception as e:  # noqa: BLE001 -- deliberately broad: any failure means not_evaluated, not a crash
        return {"status": "not_evaluated", "measured_value": None, "threshold": max_null_rate,
                "reason_not_evaluated": f"Could not profile column '{column}': {e}"}
    null_rate = (profile.null_count / profile.sampled_rows) if profile.sampled_rows else 0.0
    violated = null_rate > max_null_rate
    return {"status": _status(violated, check["severity"]), "measured_value": round(null_rate, 6),
            "threshold": max_null_rate, "reason_not_evaluated": None}


def _run_uniqueness(adapter, schema, table, check):
    params = check.get("params", {})
    columns = params.get("columns")
    if not columns:
        return {"status": "not_evaluated", "measured_value": None, "threshold": None,
                "reason_not_evaluated": "uniqueness check has no params.columns configured."}
    try:
        result = adapter.check_uniqueness(schema, table, columns)
    except Exception as e:  # noqa: BLE001
        return {"status": "not_evaluated", "measured_value": None, "threshold": "is_unique=true",
                "reason_not_evaluated": f"Could not check uniqueness on {columns}: {e}"}
    violated = not result["is_unique"]
    return {"status": _status(violated, check["severity"]),
            "measured_value": {"duplicate_count": result["duplicate_count"], "rows_checked": result["rows_checked"]},
            "threshold": "is_unique=true", "reason_not_evaluated": None}


def _run_value_range(adapter, schema, table, check):
    column = check["column"]
    params = check.get("params", {})
    lo, hi = params.get("min"), params.get("max")
    if lo is None and hi is None:
        return {"status": "not_evaluated", "measured_value": None, "threshold": None,
                "reason_not_evaluated": "value_range check has neither min nor max configured."}
    try:
        profile = adapter.profile_column(schema, table, column)
    except Exception as e:  # noqa: BLE001
        return {"status": "not_evaluated", "measured_value": None, "threshold": {"min": lo, "max": hi},
                "reason_not_evaluated": f"Could not profile column '{column}': {e}"}
    if profile.min_value is None or profile.max_value is None:
        return {"status": "not_evaluated", "measured_value": None, "threshold": {"min": lo, "max": hi},
                "reason_not_evaluated": f"Column '{column}' has no non-null values to check a range against."}
    try:
        observed_min, observed_max = float(profile.min_value), float(profile.max_value)
    except (TypeError, ValueError):
        return {"status": "not_evaluated", "measured_value": {"min": profile.min_value, "max": profile.max_value},
                "threshold": {"min": lo, "max": hi},
                "reason_not_evaluated": f"Column '{column}' values are not numeric; value_range cannot compare them."}
    violated = (lo is not None and observed_min < lo) or (hi is not None and observed_max > hi)
    return {"status": _status(violated, check["severity"]),
            "measured_value": {"observed_min": observed_min, "observed_max": observed_max},
            "threshold": {"min": lo, "max": hi}, "reason_not_evaluated": None}


def _run_referential(adapter, schema, table, check):
    column = check["column"]
    params = check.get("params", {})
    ref_object, ref_column = params.get("ref_object"), params.get("ref_column")
    max_orphan_rate = params.get("max_orphan_rate", 0)
    if not ref_object or not ref_column:
        return {"status": "not_evaluated", "measured_value": None, "threshold": None,
                "reason_not_evaluated": "referential check missing params.ref_object/ref_column."}
    ref_schema, ref_table = ref_object.split(".")[-2:]
    try:
        result = adapter.count_orphans(schema, table, column, ref_schema, ref_table, ref_column)
    except Exception as e:  # noqa: BLE001
        return {"status": "not_evaluated", "measured_value": None, "threshold": max_orphan_rate,
                "reason_not_evaluated": f"Could not check referential integrity against {ref_object}.{ref_column}: {e}"}
    violated = result["orphan_rate"] > max_orphan_rate
    return {"status": _status(violated, check["severity"]),
            "measured_value": {"orphan_count": result["orphan_count"], "orphan_rate": round(result["orphan_rate"], 6)},
            "threshold": max_orphan_rate, "reason_not_evaluated": None}


def _run_freshness(adapter, schema, table, check):
    column = check["column"]
    params = check.get("params", {})
    max_staleness_days = params.get("max_staleness_days")
    if max_staleness_days is None:
        return {"status": "not_evaluated", "measured_value": None, "threshold": None,
                "reason_not_evaluated": "freshness check has no max_staleness_days configured."}
    try:
        profile = adapter.profile_column(schema, table, column)
    except Exception as e:  # noqa: BLE001
        return {"status": "not_evaluated", "measured_value": None, "threshold": max_staleness_days,
                "reason_not_evaluated": f"Could not profile column '{column}': {e}"}
    if profile.max_value is None:
        return {"status": "not_evaluated", "measured_value": None, "threshold": max_staleness_days,
                "reason_not_evaluated": f"Column '{column}' has no non-null values to check freshness against."}
    parsed = None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(str(profile.max_value), fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    if parsed is None:
        return {"status": "not_evaluated", "measured_value": profile.max_value, "threshold": max_staleness_days,
                "reason_not_evaluated": f"Column '{column}' max value '{profile.max_value}' does not parse as a date/timestamp."}
    staleness_days = (datetime.now(timezone.utc) - parsed).days
    violated = staleness_days > max_staleness_days
    return {"status": _status(violated, check["severity"]), "measured_value": staleness_days,
            "threshold": max_staleness_days, "reason_not_evaluated": None}


def _run_custom_sql(adapter, schema, table, check):
    params = check.get("params", {})
    sql = params.get("sql")
    expected = params.get("expected", 0)
    comparison = params.get("comparison", "equals")  # equals | max | min
    if not sql:
        return {"status": "not_evaluated", "measured_value": None, "threshold": expected,
                "reason_not_evaluated": "custom_sql check has no params.sql configured."}
    try:
        value = adapter.execute_scalar(schema, sql)
    except Exception as e:  # noqa: BLE001
        return {"status": "not_evaluated", "measured_value": None, "threshold": expected,
                "reason_not_evaluated": f"custom_sql failed to execute: {e}"}
    if comparison == "equals":
        violated = value != expected
    elif comparison == "max":
        violated = value is not None and value > expected
    elif comparison == "min":
        violated = value is not None and value < expected
    else:
        return {"status": "not_evaluated", "measured_value": value, "threshold": expected,
                "reason_not_evaluated": f"Unknown comparison '{comparison}' (expected equals/max/min)."}
    return {"status": _status(violated, check["severity"]), "measured_value": value,
            "threshold": {"comparison": comparison, "expected": expected}, "reason_not_evaluated": None}


RUNNERS = {
    "row_count": _run_row_count,
    "null_rate": _run_null_rate,
    "uniqueness": _run_uniqueness,
    "value_range": _run_value_range,
    "referential": _run_referential,
    "freshness": _run_freshness,
    "custom_sql": _run_custom_sql,
}


def _definition_string(check: dict) -> str:
    if check["type"] == "custom_sql":
        return check.get("params", {}).get("sql", "")
    col = check.get("column", "")
    return f"{check['type']}({col}, {json.dumps(check.get('params', {}), default=str)})"


def run_checks(adapter: LakehouseAdapter, schema: str, table: str, checks: list[dict]) -> list[dict]:
    results = []
    for check in checks:
        runner = RUNNERS.get(check["type"])
        check_id = check.get("check_id") or f"{table}.{check.get('column', 'na')}.{check['type']}"
        if runner is None:
            outcome = {"status": "not_evaluated", "measured_value": None, "threshold": None,
                       "reason_not_evaluated": f"Unknown check type '{check['type']}'."}
        else:
            outcome = runner(adapter, schema, table, check)
        results.append({
            "check_id": check_id, "type": check["type"], "definition": _definition_string(check),
            "derived_from_contract_test": check.get("derived_from_contract_test"),
            "severity": check["severity"], **outcome,
        })
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lakehouse-dir", required=True)
    parser.add_argument("--catalog", default="acme_retail_dev")
    parser.add_argument("--schema", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("checks_json", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    checks = json.loads(args.checks_json.read_text())
    adapter = SQLiteFixtureAdapter(args.lakehouse_dir, catalog=args.catalog)
    results = run_checks(adapter, args.schema, args.table, checks)

    output = json.dumps(results, indent=2, default=str)
    if args.out:
        args.out.write_text(output)
    else:
        print(output)


if __name__ == "__main__":
    main()
