#!/usr/bin/env python3
"""
build_quality_findings.py -- the one command SKILL.md tells the agent to run. Wires together the
pre-flight cost gate, check derivation/merging (contract-derived + hand-authored, deduplicated by
check_id so a contract-derived check can be overridden by an explicit one with the same id rather
than running twice), and the deterministic scan (run_checks.py).

Produces quality_findings.json -- the deterministic half. The agent then reads this, diagnoses
every failed/warned check (root cause, confidence, suggested fix, labeled llm_inferred),
assembles the final quality-report.json, and validates it with scripts/validate_artifact.py
before declaring success. This script never diagnoses anything -- see SKILL.md step 4 and
references/toolkit-conventions.md #5.

Exit behavior: if the pre-flight cost estimate exceeds configured thresholds, this script halts
with exit code 1 and runs no checks. Never pass --force without an explicit human go-ahead.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lakehouse_adapter import SQLiteFixtureAdapter  # noqa: E402
from estimate_scan_cost import estimate_and_gate  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_checks import run_checks  # noqa: E402
from derive_checks_from_contract import derive_checks  # noqa: E402


def merge_checks(derived: list[dict], hand_authored: list[dict]) -> list[dict]:
    """Hand-authored checks win on check_id collision -- an explicit override is a deliberate
    choice, not an accident, so it should replace rather than duplicate the derived check."""
    by_id = {c["check_id"]: c for c in derived}
    for c in hand_authored:
        by_id[c["check_id"]] = c
    return list(by_id.values())


def build_findings(adapter, schema: str, table: str, checks: list[dict], thresholds: dict,
                    force: bool = False) -> dict:
    decision = estimate_and_gate(adapter, [(schema, table)], thresholds)
    if not decision["proceed"] and not force:
        return {"halted": True, "reason": "cost_threshold_exceeded", "cost_decision": decision, "checks": []}

    results = run_checks(adapter, schema, table, checks)
    summary = {"passed": 0, "failed": 0, "warned": 0, "not_evaluated": 0}
    for r in results:
        summary[r["status"]] += 1

    return {"halted": False, "cost_decision": decision, "checks": results, "summary": summary}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lakehouse-dir", required=True)
    parser.add_argument("--catalog", default="acme_retail_dev")
    parser.add_argument("--schema", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--checks-json", type=Path, default=None, help="Hand-authored check definitions.")
    parser.add_argument("--contract-json", type=Path, default=None, help="data-contract.json to derive checks from.")
    parser.add_argument("--contract-table", default=None, help="Table name within the contract (defaults to --table).")
    parser.add_argument("--max-rows-scanned", type=int, default=None)
    parser.add_argument("--max-bytes-scanned", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    derived = []
    if args.contract_json:
        contract = json.loads(args.contract_json.read_text())
        derived = derive_checks(contract, args.contract_table or args.table)

    hand_authored = json.loads(args.checks_json.read_text()) if args.checks_json else []
    checks = merge_checks(derived, hand_authored)

    adapter = SQLiteFixtureAdapter(args.lakehouse_dir, catalog=args.catalog)
    result = build_findings(
        adapter, args.schema, args.table, checks,
        thresholds={"max_rows_scanned": args.max_rows_scanned, "max_bytes_scanned": args.max_bytes_scanned},
        force=args.force,
    )

    output = json.dumps(result, indent=2, default=str)
    if args.out:
        args.out.write_text(output)
        print(f"Findings written to {args.out}" + (" (HALTED at cost gate)" if result["halted"] else ""))
    else:
        print(output)
    sys.exit(1 if result["halted"] else 0)


if __name__ == "__main__":
    main()
