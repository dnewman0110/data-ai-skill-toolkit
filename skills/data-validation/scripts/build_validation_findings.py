#!/usr/bin/env python3
"""
build_validation_findings.py -- the one command SKILL.md tells the agent to run. Wires together
the pre-flight cost gate (both source and target must clear it -- see
references/toolkit-conventions.md #4), the staged deterministic comparison (compare_staged.py),
and redaction of any raw row values that make it into the findings (scripts/redact.py, since
row_level_diff entries carry real source/target row content and must be capped/redacted before
they land in a report or get shown to an LLM for diagnosis).

Produces validation_findings.json -- the deterministic half. The agent then reads this, does the
one part that requires judgment (root-cause diagnosis per discrepancy: explanation, confidence,
suggested fix, all labeled llm_inferred), assembles the final validation-report.json, and
validates it with scripts/validate_artifact.py before declaring success. This script never
diagnoses anything itself and never suggests a fix -- see SKILL.md step 4 and
references/toolkit-conventions.md #5.

Exit behavior: if either side's pre-flight cost estimate exceeds configured thresholds, this
script halts with exit code 1 and does not compare anything. Never pass --force without an
explicit human go-ahead in the conversation.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lakehouse_adapter import SQLiteFixtureAdapter  # noqa: E402
from estimate_scan_cost import estimate_and_gate  # noqa: E402
from redact import redact_rows  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare_staged import compare  # noqa: E402


def build_findings(source_adapter, source_schema, source_table,
                    target_adapter, target_schema, target_table,
                    key_columns: list[str], thresholds: dict,
                    content_check_row_cap: int, row_level_diff_row_cap: int,
                    sensitive_columns: list[dict], compare_columns: list[str] | None = None,
                    known_acceptable_differences: list[dict] | None = None,
                    force: bool = False) -> dict:
    source_decision = estimate_and_gate(source_adapter, [(source_schema, source_table)], thresholds)
    target_decision = estimate_and_gate(target_adapter, [(target_schema, target_table)], thresholds)
    combined_proceed = source_decision["proceed"] and target_decision["proceed"]

    if not combined_proceed and not force:
        return {
            "halted": True, "reason": "cost_threshold_exceeded",
            "source_cost_decision": source_decision, "target_cost_decision": target_decision,
            "comparison": None,
        }

    result = compare(
        source_adapter, source_schema, source_table,
        target_adapter, target_schema, target_table,
        key_columns=key_columns, compare_columns=compare_columns,
        content_check_row_cap=content_check_row_cap, row_level_diff_row_cap=row_level_diff_row_cap,
        known_acceptable_differences=known_acceptable_differences or [],
    )

    # Redact raw row content in every discrepancy before it leaves this script -- these rows may
    # contain sensitive columns and are about to be handed to an LLM for diagnosis and/or written
    # into a report.
    for d in result["discrepancies"]:
        if d.get("source_row") is not None:
            d["source_row"] = redact_rows([d["source_row"]], sensitive_columns, max_records=1)[0]
        if d.get("target_row") is not None:
            d["target_row"] = redact_rows([d["target_row"]], sensitive_columns, max_records=1)[0]

    return {
        "halted": False,
        "source_cost_decision": source_decision, "target_cost_decision": target_decision,
        "comparison": result,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-lakehouse-dir", required=True)
    parser.add_argument("--source-schema", required=True)
    parser.add_argument("--source-table", required=True)
    parser.add_argument("--target-lakehouse-dir", required=True)
    parser.add_argument("--target-schema", required=True)
    parser.add_argument("--target-table", required=True)
    parser.add_argument("--key-column", action="append", required=True, help="repeatable")
    parser.add_argument("--compare-column", action="append", default=None, help="repeatable; default: intersect all columns")
    parser.add_argument("--catalog", default="acme_retail_dev")
    parser.add_argument("--max-rows-scanned", type=int, default=None)
    parser.add_argument("--max-bytes-scanned", type=int, default=None)
    parser.add_argument("--content-check-row-cap", type=int, default=100_000)
    parser.add_argument("--row-level-diff-row-cap", type=int, default=5000)
    parser.add_argument("--sensitive-columns-json", type=Path, default=None)
    parser.add_argument("--known-acceptable-differences-json", type=Path, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    source_adapter = SQLiteFixtureAdapter(args.source_lakehouse_dir, catalog=args.catalog)
    target_adapter = SQLiteFixtureAdapter(args.target_lakehouse_dir, catalog=args.catalog)

    sensitive_columns = json.loads(args.sensitive_columns_json.read_text()) if args.sensitive_columns_json else []
    known_acceptable = json.loads(args.known_acceptable_differences_json.read_text()) \
        if args.known_acceptable_differences_json else []

    result = build_findings(
        source_adapter, args.source_schema, args.source_table,
        target_adapter, args.target_schema, args.target_table,
        key_columns=args.key_column,
        thresholds={"max_rows_scanned": args.max_rows_scanned, "max_bytes_scanned": args.max_bytes_scanned},
        content_check_row_cap=args.content_check_row_cap, row_level_diff_row_cap=args.row_level_diff_row_cap,
        sensitive_columns=sensitive_columns, compare_columns=args.compare_column,
        known_acceptable_differences=known_acceptable, force=args.force,
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
