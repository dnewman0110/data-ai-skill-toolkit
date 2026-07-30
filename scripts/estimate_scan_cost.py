#!/usr/bin/env python3
"""
estimate_scan_cost.py -- pre-flight cost/blast-radius gate, shared by every skill.

references/toolkit-conventions.md #4 requires every skill to estimate scan cost BEFORE
executing anything full-scale, and to stop and ask before proceeding if it would exceed
toolkit.yaml's max_rows_scanned / max_bytes_scanned / max_wall_clock. This is one
implementation of that gate so five skills don't each write (and subtly diverge on) their own
version of "is this too expensive to just run."

Usage as a library:
    from estimate_scan_cost import estimate_and_gate
    decision = estimate_and_gate(adapter, targets=[("silver", "orders"), ("silver", "customers")],
                                  thresholds={"max_rows_scanned": 5_000_000, ...})
    if not decision["proceed"]:
        # halt, surface decision["reason"] and decision["estimated_rows"]/["estimated_bytes"]
        # to the user and ask before continuing -- do not proceed automatically.
        ...

`estimate_and_gate` never reads the target objects' row contents -- only row_count() (a fast
COUNT, or an approximate one via exact=False on backends that support it) and estimate_bytes()
(a cheap, no-full-read estimate; see lakehouse_adapter.py). This function answers "should I
proceed" without doing the expensive thing it's gating.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lakehouse_adapter import LakehouseAdapter  # noqa: E402


def estimate_and_gate(adapter: LakehouseAdapter, targets: list[tuple[str, str]],
                       thresholds: dict, wall_clock_budget_seconds: float | None = None) -> dict:
    """Returns a dict: estimated_rows, estimated_bytes, threshold results, and whether it's safe
    to proceed to full-scale execution without asking a human first.
    """
    estimated_rows = 0
    estimated_bytes = 0
    per_target = []
    for schema, table in targets:
        rows = adapter.row_count(schema, table, exact=False)
        by = adapter.estimate_bytes(schema, table)
        estimated_rows += rows
        estimated_bytes += by
        per_target.append({"object": f"{schema}.{table}", "estimated_rows": rows, "estimated_bytes": by})

    max_rows = thresholds.get("max_rows_scanned")
    max_bytes = thresholds.get("max_bytes_scanned")
    max_wall_clock = thresholds.get("max_wall_clock_seconds")

    reasons = []
    if max_rows is not None and estimated_rows > max_rows:
        reasons.append(f"estimated_rows {estimated_rows:,} exceeds max_rows_scanned {max_rows:,}")
    if max_bytes is not None and estimated_bytes > max_bytes:
        reasons.append(f"estimated_bytes {estimated_bytes:,} exceeds max_bytes_scanned {max_bytes:,}")
    if (max_wall_clock is not None and wall_clock_budget_seconds is not None
            and wall_clock_budget_seconds > max_wall_clock):
        reasons.append(
            f"requested wall-clock budget {wall_clock_budget_seconds}s exceeds max_wall_clock_seconds {max_wall_clock}s"
        )

    return {
        "proceed": len(reasons) == 0,
        "reason": "; ".join(reasons) if reasons else None,
        "estimated_rows": estimated_rows,
        "estimated_bytes": estimated_bytes,
        "per_target": per_target,
        "thresholds_applied": {
            "max_rows_scanned": max_rows, "max_bytes_scanned": max_bytes,
            "max_wall_clock_seconds": max_wall_clock,
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lakehouse-dir", required=True, help="Path to fixtures/lakehouse (or equivalent) for the SQLite fixture backend.")
    parser.add_argument("--catalog", default="acme_retail_dev")
    parser.add_argument("--target", action="append", required=True,
                         help="schema.table, repeatable, e.g. --target silver.orders --target silver.customers")
    parser.add_argument("--max-rows-scanned", type=int, default=None)
    parser.add_argument("--max-bytes-scanned", type=int, default=None)
    args = parser.parse_args()

    from lakehouse_adapter import SQLiteFixtureAdapter
    adapter = SQLiteFixtureAdapter(args.lakehouse_dir, catalog=args.catalog)
    targets = [tuple(t.split(".", 1)) for t in args.target]
    decision = estimate_and_gate(
        adapter, targets,
        thresholds={"max_rows_scanned": args.max_rows_scanned, "max_bytes_scanned": args.max_bytes_scanned},
    )
    print(json.dumps(decision, indent=2))
    sys.exit(0 if decision["proceed"] else 1)


if __name__ == "__main__":
    main()
