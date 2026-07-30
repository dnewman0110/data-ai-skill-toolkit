#!/usr/bin/env python3
"""
run_assertions.py -- deterministic checks for data-validation, runnable in CI with no subagent
and no LLM call. Covers read-only enforcement, malformed-artifact rejection, and a live smoke
test of the staged comparison engine against the fixture lakehouse's real source/target pair
(silver.orders vs gold.legacy_fct_orders) and self-comparison (silver.orders vs itself).

The two scenario evals requiring diagnosis reasoning (evals.json cases 1 and 3) are graded
separately via subagent runs -- see evals/README.md.

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
check("data-validation scripts contain no DDL/DML write statements", len(offending) == 0)
for o in offending:
    print(f"    {o}")

# -- 2. Malformed / unsupported-major artifact rejected cleanly. --
if (REPO_ROOT / "contracts" / "examples" / "validation-report.example.json").exists():
    result = run([
        sys.executable, "scripts/validate_artifact.py",
        "contracts/examples/validation-report.example.json",
        "--schema-type", "validation-report", "--supported-major", "99",
    ], expect_success=False)
    check("Artifact with unsupported major version is refused",
          result.returncode != 0 and "supports major version" in (result.stdout + result.stderr))

bad_artifact = {"schema_version": "1.0.0", "run": {}, "source": {}}
bad_path = Path("/tmp/bad_validation_report.json")
bad_path.write_text(json.dumps(bad_artifact))
result = run([sys.executable, "scripts/validate_artifact.py", str(bad_path), "--schema-type", "validation-report"],
             expect_success=False)
check("Structurally invalid artifact (missing required fields) is rejected", result.returncode != 0)

# -- 3. Live smoke test against the fixture lakehouse. --
if LAKEHOUSE_DIR.exists():
    sys.path.insert(0, str(SKILL_DIR / "scripts"))
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from lakehouse_adapter import SQLiteFixtureAdapter  # noqa: E402
    from compare_staged import compare  # noqa: E402

    adapter = SQLiteFixtureAdapter(str(LAKEHOUSE_DIR))

    # 3a. Real discrepancy: silver.orders vs gold.legacy_fct_orders
    result = compare(adapter, "silver", "orders", adapter, "gold", "legacy_fct_orders",
                      key_columns=["order_id", "line_number"])
    check("Comparison reaches row_level_diff (real discrepancy exists)",
          result["summary"]["deepest_stage_reached"] == "row_level_diff")
    check("Exactly one discrepancy found", len(result["discrepancies"]) == 1)
    if result["discrepancies"]:
        d = result["discrepancies"][0]
        check("Discrepancy kind is missing_from_target", d["kind"] == "missing_from_target")
        check("Discrepancy key is order_id=5230, line_number=1",
              d["key"] == {"order_id": 5230, "line_number": 1})
    check("No false positive on total_amt (type coercion works end-to-end)",
          not any("total_amt" in d.get("columns_affected", []) for d in result["discrepancies"]))

    # 3b. Clean match: silver.orders vs itself, should stop at hash_aggregate
    self_result = compare(adapter, "silver", "orders", adapter, "silver", "orders",
                           key_columns=["order_id", "line_number"])
    check("Self-comparison matches", self_result["summary"]["match"] is True)
    check("Self-comparison stops at hash_aggregate (doesn't over-scan)",
          self_result["summary"]["deepest_stage_reached"] == "hash_aggregate")
    stage_names_executed = {s["stage"]: s["executed"] for s in self_result["stages"]}
    check("column_aggregate and row_level_diff not executed when hash matches",
          not stage_names_executed["column_aggregate"] and not stage_names_executed["row_level_diff"])

    # 3c. known_acceptable_differences exclusion
    excl_result = compare(adapter, "silver", "orders", adapter, "gold", "legacy_fct_orders",
                           key_columns=["order_id", "line_number"],
                           known_acceptable_differences=[
                               {"type": "key_ignore", "key": [5230, 1], "description": "test", "declared_by": "test"}
                           ])
    check("known_acceptable_differences excludes the declared key",
          len(excl_result["discrepancies"]) == 0 and len(excl_result["known_acceptable_differences_excluded"]) == 1)
    check("Excluded-but-clean run reports match true",
          excl_result["summary"]["match"] is True)
else:
    print("[SKIP] Live fixture smoke test -- run fixtures/generate_fixtures.py first.")

print()
if failures:
    print(f"FAIL: {len(failures)} assertion(s) failed.")
    sys.exit(1)
else:
    print("PASS: all deterministic assertions passed.")
    sys.exit(0)
