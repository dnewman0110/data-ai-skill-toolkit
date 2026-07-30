#!/usr/bin/env python3
"""
propose_tests.py -- deterministic derivation of data-contract tests[] from a profile_object.py
result. No LLM, no judgment calls about business meaning -- every threshold traces back to
either a declared constraint or a number computed from the profiled sample, per
contracts/confidence-rubric.md's threshold_basis values (explicit_constraint / profiled).

Also returns `findings` -- a list of plain-language flags for things this pass noticed that a
human (or the agent assembling the final contract) should see in the contract's assumptions[],
because a proposed test alone doesn't communicate "this is currently failing" or "this looks
like a type mismatch." Findings are facts with evidence, not inferences -- they still don't
require an LLM to produce, but they DO belong in assumptions[] per toolkit-conventions.md #6
("never silently infer... every inference lands in assumptions[]") since a downstream reader
needs the *interpretation* "this might be a defect" even though the *detection* was automatic.

Thresholds this module invents zero of: nullability thresholds come from the observed null
rate (rounded up for headroom) or 0 for enforced/always-clean columns; range thresholds come
from observed min/max with a fixed, documented headroom multiplier; referential/uniqueness
tests are pass/fail with no invented tolerance. The only toolkit.yaml-configured input is the
null_rate_warning band used to decide whether a nonzero observed null rate is worth flagging as
a possible defect (default 0.01-0.10, i.e. "small but not obviously intentional") -- that band
is a policy choice, not a threshold on any individual test.
"""
import argparse
import json
import math
import sys
from pathlib import Path

RANGE_HEADROOM = 0.10  # 10% beyond observed min/max, fixed and documented, not a magic number
NULLABLE_FLAG_UPPER_BOUND = 0.10  # above this, a nonzero null rate reads as clearly intentional


def _looks_like_identifier(column_name: str) -> bool:
    return column_name.endswith("_id") or column_name.endswith("_number") or column_name.endswith("_code")


def propose_tests(profile: dict, null_rate_flag_lower_bound: float = 0.0) -> dict:
    tests = []
    findings = []
    declared_pk = set(profile.get("declared_primary_key") or [])
    declared_not_null = set(profile.get("declared_not_null") or [])
    fk_columns = {fk["column"] for fk in profile.get("fk_checks", [])}

    # -- uniqueness, from candidate_keys --
    for ck in profile.get("candidate_keys", []):
        cols = ck["columns"]
        col_label = ",".join(cols)
        if ck["source"] == "declared_primary_key":
            threshold_basis, severity = "explicit_constraint", "blocking"
        elif ck["source"] == "natural_key_naming_heuristic":
            threshold_basis, severity = "profiled", "blocking"
        else:
            # first_column_only / first_two_columns -- a positional guess when no PK is declared,
            # weaker evidence than a name match or a real constraint.
            threshold_basis, severity = "profiled", "warning"

        tests.append({
            "type": "uniqueness", "column": col_label,
            "params": {"columns": cols},
            "threshold_basis": threshold_basis, "severity": severity,
        })
        if not ck["is_unique"]:
            findings.append({
                "statement": f"Candidate key ({col_label}) is NOT currently unique: "
                              f"{ck['duplicate_count']} duplicate value(s) found across "
                              f"{ck['rows_checked']} row(s) checked "
                              f"({'full population' if ck['checked_full_population'] else 'sample'}).",
                "basis": f"profiled: check_uniqueness on ({col_label}), source={ck['source']}.",
            })

    # -- nullability, from column profiles --
    for col in profile.get("columns", []):
        name = col["name"]
        null_rate = col["null_rate"]
        if name in declared_not_null:
            tests.append({
                "type": "nullability", "column": name, "params": {"max_null_rate": 0},
                "threshold_basis": "explicit_constraint", "severity": "blocking",
            })
        elif null_rate == 0.0:
            tests.append({
                "type": "nullability", "column": name, "params": {"max_null_rate": 0},
                "threshold_basis": "profiled", "severity": "warning",
            })
        elif null_rate_flag_lower_bound <= null_rate <= NULLABLE_FLAG_UPPER_BOUND:
            # Observed rate becomes the monitoring threshold going forward (headroom rounds up to
            # the next whole percentage point so normal sampling noise doesn't immediately trip
            # it) -- derived from profiling, not invented -- AND flagged as worth a human look,
            # since a small-but-nonzero null rate on a column with no NOT NULL constraint is
            # exactly the "nullable column that shouldn't be" shape of problem.
            headroom_rate = math.ceil(null_rate * 100) / 100
            tests.append({
                "type": "nullability", "column": name, "params": {"max_null_rate": headroom_rate},
                "threshold_basis": "profiled", "severity": "warning",
            })
            findings.append({
                "statement": f"Column '{name}' has no NOT NULL constraint and a nonzero-but-low "
                              f"observed null rate ({null_rate:.1%}, {col['null_count']} of "
                              f"{col['sampled_rows']} rows). This is a common shape for a data "
                              f"quality defect rather than intentional optionality -- recommend "
                              f"human review of whether these rows are legitimate.",
                "basis": f"profiled: null_rate {null_rate:.4f} on '{name}', "
                         f"{'full population' if col['sample_covers_full_population'] else 'sample'}.",
            })
        # else: null_rate above NULLABLE_FLAG_UPPER_BOUND reads as clearly-intentional optionality,
        # no test proposed, no flag raised.

        if col.get("looks_numeric_but_declared_text"):
            findings.append({
                "statement": f"Column '{name}' is declared {col['declared_type']} but every "
                              f"sampled non-null value parses as a number (e.g. min='{col['min_value']}', "
                              f"max='{col['max_value']}' by lexicographic string comparison, not numeric "
                              f"comparison -- treat those two values with caution). Likely needs an "
                              f"explicit CAST when mapped to a numeric target column.",
                "basis": f"measured: 100% of sampled non-null values in '{name}' parse as float "
                         f"despite a TEXT/VARCHAR declared type.",
            })

    # -- referential, from fk_checks --
    for fk in profile.get("fk_checks", []):
        threshold_basis = "explicit_constraint" if fk["declared"] else "profiled"
        severity = "blocking" if fk["orphan_count"] == 0 else "warning"
        tests.append({
            "type": "referential", "column": fk["column"],
            "params": {"ref_object": fk["ref_object"], "ref_column": fk["ref_column"]},
            "threshold_basis": threshold_basis, "severity": severity,
        })
        if fk["orphan_count"] > 0:
            findings.append({
                "statement": f"{fk['orphan_count']} row(s) in this table have a '{fk['column']}' "
                              f"value with no match in {fk['ref_object']}.{fk['ref_column']} "
                              f"(orphan rate {fk['orphan_rate']:.2%} of {fk['rows_checked']} checked).",
                "basis": f"profiled: count_orphans({fk['column']} -> {fk['ref_object']}.{fk['ref_column']}).",
            })

    # -- range, from column profiles: numeric, non-identifier, non-key measure-shaped columns --
    for col in profile.get("columns", []):
        name = col["name"]
        if name in declared_pk or name in fk_columns or _looks_like_identifier(name):
            continue
        if not col["is_declared_numeric"]:
            continue
        if col["min_value"] is None or col["max_value"] is None:
            continue
        lo, hi = float(col["min_value"]), float(col["max_value"])
        span = hi - lo
        headroom = span * RANGE_HEADROOM if span > 0 else max(abs(hi) * RANGE_HEADROOM, 1)
        tests.append({
            "type": "range", "column": name,
            "params": {"min": round(lo - headroom, 4), "max": round(hi + headroom, 4),
                       "observed_min": lo, "observed_max": hi},
            "threshold_basis": "profiled", "severity": "warning",
        })

    return {"tests": tests, "findings": findings}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("profile_json", type=Path, help="Output of profile_object.py")
    parser.add_argument("--null-rate-flag-lower-bound", type=float, default=0.0)
    args = parser.parse_args()

    profile = json.loads(args.profile_json.read_text())
    result = propose_tests(profile, args.null_rate_flag_lower_bound)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
