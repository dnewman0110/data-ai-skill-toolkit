#!/usr/bin/env python3
"""
run_assertions.py -- deterministic checks for data-quality, runnable in CI with no subagent and
no LLM call. Covers read-only enforcement (including the custom_sql SQL-injection-shaped guard),
malformed-artifact rejection, and a live smoke test of run_checks.py + derive_checks_from_contract.py
against the fixture lakehouse.

The scenario evals requiring diagnosis reasoning are graded separately via subagent runs -- see
evals/README.md.

Exit 0 if every check passes, 1 otherwise.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = Path(__file__).resolve().parents[1]
LAKEHOUSE_DIR = REPO_ROOT / "fixtures" / "lakehouse"

failures = []


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


def run(cmd, expect_success=True):
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if expect_success and result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
    return result


# -- 1. Read-only enforcement: static scan of this skill's shipped scripts. --
write_keywords = re.compile(r"\b(INSERT INTO|UPDATE |DELETE FROM|DROP TABLE|CREATE TABLE|ALTER TABLE|TRUNCATE)\b", re.IGNORECASE)
scripts_dir = SKILL_DIR / "scripts"
offending = []
for py_file in scripts_dir.glob("*.py"):
    text = py_file.read_text()
    for m in write_keywords.finditer(text):
        offending.append(f"{py_file.name}: found '{m.group(0)}'")
check("data-quality scripts contain no DDL/DML write statements", len(offending) == 0)
for o in offending:
    print(f"    {o}")

# -- 2. Malformed / unsupported-major artifact rejected cleanly. --
if (REPO_ROOT / "contracts" / "examples" / "quality-report.example.json").exists():
    result = run([
        sys.executable, "scripts/validate_artifact.py",
        "contracts/examples/quality-report.example.json",
        "--schema-type", "quality-report", "--supported-major", "99",
    ], expect_success=False)
    check("Artifact with unsupported major version is refused",
          result.returncode != 0 and "supports major version" in (result.stdout + result.stderr))

bad_artifact = {"schema_version": "1.0.0", "run": {}, "target_object": "x"}
bad_path = Path("/tmp/bad_quality_report.json")
bad_path.write_text(json.dumps(bad_artifact))
result = run([sys.executable, "scripts/validate_artifact.py", str(bad_path), "--schema-type", "quality-report"],
             expect_success=False)
check("Structurally invalid artifact (missing required fields) is rejected", result.returncode != 0)

# -- 3. custom_sql guard: write-shaped SQL is rejected before execution. --
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from lakehouse_adapter import SQLiteFixtureAdapter, assert_read_only_select  # noqa: E402

for bad_sql in ["DROP TABLE silver.orders", "DELETE FROM silver.orders", "SELECT 1; DROP TABLE silver.orders",
                "UPDATE silver.orders SET total_amt = 0"]:
    try:
        assert_read_only_select(bad_sql)
        check(f"custom_sql guard rejects: {bad_sql[:40]}", False)
    except ValueError:
        check(f"custom_sql guard rejects: {bad_sql[:40]}", True)
try:
    assert_read_only_select("SELECT COUNT(*) FROM silver.orders")
    check("custom_sql guard accepts a genuine SELECT", True)
except ValueError:
    check("custom_sql guard accepts a genuine SELECT", False)

# -- 4. Live smoke test against the fixture lakehouse. --
if LAKEHOUSE_DIR.exists():
    sys.path.insert(0, str(SKILL_DIR / "scripts"))
    from run_checks import run_checks  # noqa: E402
    from derive_checks_from_contract import derive_checks  # noqa: E402

    adapter = SQLiteFixtureAdapter(str(LAKEHOUSE_DIR))

    orders_checks = [
        {"check_id": "orders.ship_region.null_rate", "type": "null_rate", "column": "ship_region",
         "params": {"max_null_rate": 0}, "severity": "warning"},
        {"check_id": "orders.pk.uniqueness", "type": "uniqueness",
         "params": {"columns": ["order_id", "line_number"]}, "severity": "blocking"},
        {"check_id": "orders.customer_id.referential", "type": "referential", "column": "customer_id",
         "params": {"ref_object": "silver.customers", "ref_column": "customer_id", "max_orphan_rate": 0},
         "severity": "warning"},
        {"check_id": "orders.no_threshold.row_count", "type": "row_count", "params": {}, "severity": "warning"},
        {"check_id": "orders.missing_col.null_rate", "type": "null_rate", "column": "does_not_exist",
         "params": {"max_null_rate": 0}, "severity": "warning"},
    ]
    results = run_checks(adapter, "silver", "orders", orders_checks)
    by_id = {r["check_id"]: r for r in results}

    check("null_rate check on ship_region is 'warned' (planted flaw)",
          by_id["orders.ship_region.null_rate"]["status"] == "warned")
    check("uniqueness check on (order_id, line_number) is 'passed'",
          by_id["orders.pk.uniqueness"]["status"] == "passed")
    check("referential check on customer_id is 'warned' (planted orphan)",
          by_id["orders.customer_id.referential"]["status"] == "warned")
    check("row_count with no threshold is 'not_evaluated', not a silent pass",
          by_id["orders.no_threshold.row_count"]["status"] == "not_evaluated")
    check("null_rate check on a nonexistent column is 'not_evaluated' with a reason",
          by_id["orders.missing_col.null_rate"]["status"] == "not_evaluated"
          and bool(by_id["orders.missing_col.null_rate"]["reason_not_evaluated"]))

    customers_checks = [
        {"check_id": "customers.customer_number.uniqueness", "type": "uniqueness",
         "params": {"columns": ["customer_number"]}, "severity": "blocking"},
    ]
    customers_results = run_checks(adapter, "silver", "customers", customers_checks)
    check("uniqueness check on customer_number is 'failed' (planted duplicate natural key)",
          customers_results[0]["status"] == "failed")

    # Contract-derived checks actually execute against real data, not just echo the contract.
    contract = json.loads((REPO_ROOT / "contracts" / "examples" / "data-contract.example.json").read_text())
    derived = derive_checks(contract, "fct_orders")
    derived_results = run_checks(adapter, "silver", "orders", derived)
    check("Contract-derived checks execute and produce real statuses (not all not_evaluated)",
          any(r["status"] != "not_evaluated" for r in derived_results))
    check("Every derived check carries derived_from_contract_test back to the source contract",
          all(r["derived_from_contract_test"] is not None for r in derived_results))
else:
    print("[SKIP] Live fixture smoke test -- run fixtures/generate_fixtures.py first.")

print()
if failures:
    print(f"FAIL: {len(failures)} assertion(s) failed.")
    sys.exit(1)
else:
    print("PASS: all deterministic assertions passed.")
    sys.exit(0)
