#!/usr/bin/env python3
"""
build_model_findings.py -- the one command SKILL.md tells the agent to run before designing
anything. Wires together: the pre-flight cost gate (toolkit-conventions.md #4 -- profiling touches
real data, same as every other skill), silver_verification (the gold-layer-only refusal gate,
checked FIRST and short-circuiting everything else if it fails), then -- only if verification
passes -- per-fact grain validation, per-dimension SCD candidate detection, and conformed-dimension
candidate discovery.

Produces model_findings.json -- the deterministic half. The agent then reads this and does the
part that is irreducibly judgment: classifying measure additivity, choosing SCD types with a
rationale, deciding conformance groups, and assembling the actual facts[]/dimensions[] design --
see SKILL.md steps 2-4 and references/toolkit-conventions.md #5. This script never proposes a
fact, a dimension, a measure, or an SCD type; it only measures whether the ground beneath a
proposal is solid.

Exit behavior: if the cost gate is exceeded, OR if silver_verification fails for any referenced
source object, this halts (exit code 1) with `halted: true` and does not run grain/SCD/conformance
checks -- there is nothing safe to check further once the source layer itself is refused. Never
pass --force on the silver_verification failure without an explicit human decision to proceed
anyway (see references/silver-verification.md for when overriding might legitimately happen and
what must accompany it in assumptions[]).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lakehouse_adapter import SQLiteFixtureAdapter  # noqa: E402
from estimate_scan_cost import estimate_and_gate  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_silver_layer import verify_silver_layer  # noqa: E402
from validate_grain_against_measures import validate_grain  # noqa: E402
from detect_scd_candidates import detect_scd_candidates  # noqa: E402
from derive_conformance_candidates import derive_conformance_candidates  # noqa: E402


def build_model_findings(adapter, source_objects: list[tuple], thresholds: dict,
                          fact_specs: list[dict] | None = None, dimension_tables: list[tuple] | None = None,
                          proposed_dimension_names: list[str] | None = None, gold_schema: str = "gold",
                          force_past_verification: bool = False) -> dict:
    cost_decision = estimate_and_gate(adapter, source_objects, thresholds)
    if not cost_decision["proceed"] and not force_past_verification:
        return {"halted": True, "reason": "cost_threshold_exceeded", "cost_decision": cost_decision,
                "silver_verification": None, "fact_grain_checks": None, "scd_candidates": None,
                "conformance_candidates": None}

    verification = verify_silver_layer(adapter, source_objects)
    if not verification["verified"] and not force_past_verification:
        return {"halted": True, "reason": "silver_verification_failed", "cost_decision": cost_decision,
                "silver_verification": verification, "fact_grain_checks": None, "scd_candidates": None,
                "conformance_candidates": None}

    fact_grain_checks = []
    for spec in (fact_specs or []):
        fact_grain_checks.append(validate_grain(
            adapter, spec["schema"], spec["table"], spec["grain_columns"], spec.get("measure_columns", [])
        ))

    scd_candidates = []
    for schema, table in (dimension_tables or []):
        scd_candidates.append(detect_scd_candidates(adapter, schema, table))

    conformance_candidates = None
    if proposed_dimension_names:
        conformance_candidates = derive_conformance_candidates(adapter, gold_schema, proposed_dimension_names)

    return {
        "halted": False,
        "cost_decision": cost_decision,
        "silver_verification": verification,
        "fact_grain_checks": fact_grain_checks,
        "scd_candidates": scd_candidates,
        "conformance_candidates": conformance_candidates,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lakehouse-dir", required=True)
    parser.add_argument("--catalog", default="acme_retail_dev")
    parser.add_argument("--source-object", action="append", required=True, help="schema.table, repeatable -- every silver object this model would source from")
    parser.add_argument("--fact-spec-json", type=Path, default=None,
                         help="JSON file: list of {schema, table, grain_columns, measure_columns}")
    parser.add_argument("--dimension-table", action="append", default=[], help="schema.table, repeatable -- checked for SCD history-table evidence")
    parser.add_argument("--proposed-dimension-name", action="append", default=[], help="repeatable")
    parser.add_argument("--gold-schema", default="gold")
    parser.add_argument("--max-rows-scanned", type=int, default=None)
    parser.add_argument("--max-bytes-scanned", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    adapter = SQLiteFixtureAdapter(args.lakehouse_dir, catalog=args.catalog)
    source_objects = [tuple(spec.split(".", 1)) for spec in args.source_object]
    dimension_tables = [tuple(spec.split(".", 1)) for spec in args.dimension_table]
    fact_specs = json.loads(args.fact_spec_json.read_text()) if args.fact_spec_json else []

    result = build_model_findings(
        adapter, source_objects,
        thresholds={"max_rows_scanned": args.max_rows_scanned, "max_bytes_scanned": args.max_bytes_scanned},
        fact_specs=fact_specs, dimension_tables=dimension_tables,
        proposed_dimension_names=args.proposed_dimension_name, gold_schema=args.gold_schema,
        force_past_verification=args.force,
    )

    output = json.dumps(result, indent=2, default=str)
    if args.out:
        args.out.write_text(output)
        print(f"Findings written to {args.out}" + (" (HALTED)" if result["halted"] else ""))
    else:
        print(output)
    sys.exit(1 if result["halted"] else 0)


if __name__ == "__main__":
    main()
