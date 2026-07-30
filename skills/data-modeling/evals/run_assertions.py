#!/usr/bin/env python3
"""
run_assertions.py -- deterministic checks for data-modeling, runnable in CI with no subagent and
no LLM call. Covers read-only enforcement, malformed-artifact rejection, the silver-verification
gate's five signals against the real fixture lakehouse (correctly REFUSING bronze.raw_orders and
correctly VERIFYING silver.orders/silver.customers despite their planted data-quality flaws --
the structural-vs-quality distinction this skill's whole design rests on), grain validation,
SCD candidate detection (finds the real customer_region_history fixture), conformance candidate
discovery, and a full end-to-end orchestrator smoke test on both the happy path and the refusal
path.

The scenario evals requiring design judgment (measure additivity, SCD rationale, conformance
decisions) are graded separately via subagent runs -- see evals/README.md.

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
check("data-modeling scripts contain no DDL/DML write statements", len(offending) == 0)
for o in offending:
    print(f"    {o}")

# -- 2. Malformed / unsupported-major artifact rejected cleanly; shipped example passes. --
result = run([
    sys.executable, "scripts/validate_artifact.py",
    "contracts/examples/model-spec.example.json",
    "--schema-type", "model-spec", "--supported-major", "99",
], expect_success=False)
check("Artifact with unsupported major version is refused",
      result.returncode != 0 and "supports major version" in (result.stdout + result.stderr))

bad_artifact = {"schema_version": "1.0.0", "run": {}, "model_id": "x"}
bad_path = Path("/tmp/bad_model_spec.json")
bad_path.write_text(json.dumps(bad_artifact))
result = run([sys.executable, "scripts/validate_artifact.py", str(bad_path), "--schema-type", "model-spec"],
             expect_success=False)
check("Structurally invalid artifact (missing required fields) is rejected", result.returncode != 0)

result = run([sys.executable, "scripts/validate_artifact.py",
              "contracts/examples/model-spec.example.json", "--schema-type", "model-spec", "--supported-major", "1"])
check("Shipped model-spec.example.json validates cleanly", result.returncode == 0)

# -- 3. Live checks against the fixture lakehouse. --
if LAKEHOUSE_DIR.exists():
    sys.path.insert(0, str(SKILL_DIR / "scripts"))
    from verify_silver_layer import verify_silver_layer  # noqa: E402
    from validate_grain_against_measures import validate_grain  # noqa: E402
    from detect_scd_candidates import detect_scd_candidates  # noqa: E402
    from derive_conformance_candidates import derive_conformance_candidates  # noqa: E402
    from build_model_findings import build_model_findings  # noqa: E402
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from lakehouse_adapter import SQLiteFixtureAdapter  # noqa: E402

    adapter = SQLiteFixtureAdapter(str(LAKEHOUSE_DIR))

    bronze_result = verify_silver_layer(adapter, [("bronze", "raw_orders")])
    check("bronze.raw_orders is correctly REFUSED (verified: false)", bronze_result["verified"] is False)
    check("bronze.raw_orders is classified bronze_or_raw", bronze_result["layer_detected"] == "bronze_or_raw")
    check("bronze.raw_orders reason_if_not_verified names the raw-ingestion columns",
          "_rescued_data" in bronze_result["reason_if_not_verified"])

    silver_result = verify_silver_layer(adapter, [("silver", "orders"), ("silver", "customers")])
    check("silver.orders + silver.customers are VERIFIED despite their planted data-quality flaws "
          "(broken FK, dup natural key, nullable-that-shouldn't-be) -- this is the structural-vs-"
          "quality distinction the whole skill rests on",
          silver_result["verified"] is True)
    check("silver.orders + silver.customers are classified silver_curated", silver_result["layer_detected"] == "silver_curated")

    gold_result = verify_silver_layer(adapter, [("gold", "legacy_fct_orders")])
    check("An object already in gold short-circuits to verified/gold without running the 5 signals",
          gold_result["verified"] is True and gold_result["layer_detected"] == "gold"
          and gold_result["per_object"][0]["signals"] == {})

    grain_ok = validate_grain(adapter, "silver", "orders", ["order_id", "line_number"], ["quantity", "total_amt"])
    check("Correct order-line grain (order_id, line_number) validates as unique", grain_ok["grain_holds"] is True)
    check("Measure total_amt's declared type (TEXT) is surfaced so additivity classification can't silently assume numeric",
          next(m for m in grain_ok["measures"] if m["column"] == "total_amt")["declared_type"] == "TEXT")

    grain_bad = validate_grain(adapter, "silver", "orders", ["customer_id"], [])
    check("An incorrect grain (customer_id alone -- multiple order lines per customer) correctly fails uniqueness",
          grain_bad["grain_holds"] is False)

    scd_customers = detect_scd_candidates(adapter, "silver", "customers")
    check("SCD candidate detection finds the real customer_region_history fixture for silver.customers",
          "customer_region_history" in scd_customers["history_tables_found"])
    scd_orders = detect_scd_candidates(adapter, "silver", "orders")
    check("SCD candidate detection finds nothing for silver.orders (no history table exists for it)",
          scd_orders["history_tables_found"] == [])

    conformance = derive_conformance_candidates(adapter, "gold", ["legacy_fct_orders", "customer"])
    check("Conformance check does not false-positive-match a non-dimension-shaped gold table (legacy_fct_orders)",
          conformance["candidates"]["legacy_fct_orders"] == [])

    # -- 4. End-to-end orchestrator: refusal path and happy path. --
    refused = build_model_findings(adapter, [("bronze", "raw_orders")], thresholds={})
    check("build_model_findings halts on an unverified source with reason silver_verification_failed",
          refused["halted"] is True and refused["reason"] == "silver_verification_failed")

    ok = build_model_findings(
        adapter, [("silver", "orders"), ("silver", "customers")], thresholds={},
        fact_specs=[{"schema": "silver", "table": "orders", "grain_columns": ["order_id", "line_number"],
                     "measure_columns": ["quantity", "total_amt"]}],
        dimension_tables=[("silver", "customers")],
        proposed_dimension_names=["customer"],
    )
    check("build_model_findings succeeds end-to-end on verified silver sources", ok["halted"] is False)
    check("build_model_findings' fact_grain_checks reports grain_holds True for the real order-line grain",
          ok["fact_grain_checks"][0]["grain_holds"] is True)
    check("build_model_findings' scd_candidates surfaces customer_region_history",
          "customer_region_history" in ok["scd_candidates"][0]["history_tables_found"])
else:
    print("[SKIP] Live fixture tests -- run fixtures/generate_fixtures.py first.")

print()
if failures:
    print(f"FAIL: {len(failures)} assertion(s) failed.")
    sys.exit(1)
else:
    print("PASS: all deterministic assertions passed.")
    sys.exit(0)
