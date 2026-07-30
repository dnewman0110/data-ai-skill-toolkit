#!/usr/bin/env python3
"""
diff_artifact.py -- structured diff between two versions of the same artifact type, so a
re-run never leaves a team eyeballing two JSON files to find out what changed
(toolkit-conventions.md #8, idempotency and re-runs). Every skill's re-run path calls this
against its own previous run (located via run.previous_run_id) and writes the result alongside
the new artifact as <run_id>.diff.json.

Currently implements the data-contract diff (columns/tests/mappings added, removed, changed --
exactly the things the spec calls out). Other artifact types (model-spec, quality-report,
validation-report) get their own diff_<type> function here as Phase 2 builds those skills out,
registered in DIFFERS below -- the CLI and the "nothing changed" short-circuit are shared.
"""
import argparse
import json
import sys
from pathlib import Path


def _index_by(items: list[dict], key) -> dict:
    return {key(item): item for item in items}


def diff_data_contract(old: dict, new: dict) -> dict:
    diff = {"tables": {"added": [], "removed": [], "changed": []}}
    old_tables = _index_by(old.get("tables", []), lambda t: t["name"])
    new_tables = _index_by(new.get("tables", []), lambda t: t["name"])

    for name in new_tables.keys() - old_tables.keys():
        diff["tables"]["added"].append(name)
    for name in old_tables.keys() - new_tables.keys():
        diff["tables"]["removed"].append(name)

    for name in old_tables.keys() & new_tables.keys():
        old_t, new_t = old_tables[name], new_tables[name]
        table_diff = {"columns": {"added": [], "removed": [], "changed": []},
                      "tests": {"added": [], "removed": [], "changed": []}}

        old_cols = _index_by(old_t.get("columns", []), lambda c: c["name"])
        new_cols = _index_by(new_t.get("columns", []), lambda c: c["name"])
        table_diff["columns"]["added"] = sorted(new_cols.keys() - old_cols.keys())
        table_diff["columns"]["removed"] = sorted(old_cols.keys() - new_cols.keys())
        for col in sorted(old_cols.keys() & new_cols.keys()):
            o, n = old_cols[col], new_cols[col]
            changes = {}
            if o.get("type") != n.get("type"):
                changes["type"] = {"old": o.get("type"), "new": n.get("type")}
            if o.get("nullable") != n.get("nullable"):
                changes["nullable"] = {"old": o.get("nullable"), "new": n.get("nullable")}
            old_src, new_src = o.get("source", {}), n.get("source", {})
            if (old_src.get("object"), old_src.get("column")) != (new_src.get("object"), new_src.get("column")):
                changes["source_mapping"] = {
                    "old": f"{old_src.get('object')}.{old_src.get('column')}",
                    "new": f"{new_src.get('object')}.{new_src.get('column')}",
                }
            if old_src.get("confidence") != new_src.get("confidence"):
                changes["mapping_confidence"] = {"old": old_src.get("confidence"), "new": new_src.get("confidence")}
            if changes:
                table_diff["columns"]["changed"].append({"column": col, "changes": changes})

        def test_key(t):
            return (t["type"], t["column"])

        old_tests = _index_by(old_t.get("tests", []), test_key)
        new_tests = _index_by(new_t.get("tests", []), test_key)
        table_diff["tests"]["added"] = [f"{k[0]}:{k[1]}" for k in new_tests.keys() - old_tests.keys()]
        table_diff["tests"]["removed"] = [f"{k[0]}:{k[1]}" for k in old_tests.keys() - new_tests.keys()]
        for k in old_tests.keys() & new_tests.keys():
            if old_tests[k].get("params") != new_tests[k].get("params") or \
               old_tests[k].get("severity") != new_tests[k].get("severity"):
                table_diff["tests"]["changed"].append({
                    "test": f"{k[0]}:{k[1]}",
                    "old": {"params": old_tests[k].get("params"), "severity": old_tests[k].get("severity")},
                    "new": {"params": new_tests[k].get("params"), "severity": new_tests[k].get("severity")},
                })

        if any(table_diff["columns"].values()) or any(table_diff["tests"].values()):
            diff["tables"]["changed"].append({"table": name, **table_diff})

    return diff


def diff_validation_report(old: dict, new: dict) -> dict:
    """Discrepancies aren't identified by a stable name the way columns/tests are -- they're
    identified by (kind, key). Diff by that composite key so a re-run shows exactly which
    specific discrepancies are new, which were resolved since last time, and which persist."""
    def discrepancy_key(d):
        return (d.get("kind"), tuple(sorted(d.get("key", {}).items())))

    old_discrepancies = _index_by(old.get("discrepancies", []), discrepancy_key)
    new_discrepancies = _index_by(new.get("discrepancies", []), discrepancy_key)

    def _readable(k):
        return {"kind": k[0], "key": dict(k[1])}

    resolved = [_readable(k) for k in old_discrepancies.keys() - new_discrepancies.keys()]
    new_found = [_readable(k) for k in new_discrepancies.keys() - old_discrepancies.keys()]
    persisting = []
    for k in old_discrepancies.keys() & new_discrepancies.keys():
        old_d, new_d = old_discrepancies[k], new_discrepancies[k]
        if old_d.get("columns_affected") != new_d.get("columns_affected"):
            persisting.append({"kind": k[0], "key": dict(k[1]),
                                "columns_affected_change": {"old": old_d.get("columns_affected"),
                                                             "new": new_d.get("columns_affected")}})

    old_summary = old.get("summary", {})
    new_summary = new.get("summary", {})

    return {
        "discrepancies": {"resolved_since_last_run": resolved, "newly_found": new_found,
                           "persisting_with_changes": persisting},
        "match_status": {"old": old_summary.get("match"), "new": new_summary.get("match")},
        "deepest_stage_reached": {"old": old_summary.get("deepest_stage_reached"),
                                   "new": new_summary.get("deepest_stage_reached")},
    }


def diff_quality_report(old: dict, new: dict) -> dict:
    """Checks ARE stably named (check_id), unlike validation's row-keyed discrepancies, so this
    is closer to the data-contract differ: index by check_id, report added/removed checks and
    status transitions for checks present in both runs. A status transition is the single most
    actionable thing a re-run can tell a team -- "this started failing" or "this got fixed" --
    so it's surfaced explicitly rather than buried in a full before/after check list."""
    old_checks = _index_by(old.get("checks", []), lambda c: c["check_id"])
    new_checks = _index_by(new.get("checks", []), lambda c: c["check_id"])

    added = sorted(new_checks.keys() - old_checks.keys())
    removed = sorted(old_checks.keys() - new_checks.keys())
    status_changes = []
    for check_id in sorted(old_checks.keys() & new_checks.keys()):
        old_c, new_c = old_checks[check_id], new_checks[check_id]
        if old_c.get("status") != new_c.get("status"):
            status_changes.append({
                "check_id": check_id,
                "status": {"old": old_c.get("status"), "new": new_c.get("status")},
                "measured_value": {"old": old_c.get("measured_value"), "new": new_c.get("measured_value")},
            })

    old_summary = old.get("summary", {})
    new_summary = new.get("summary", {})

    return {
        "checks": {"added": added, "removed": removed, "status_changes": status_changes},
        "summary": {"old": old_summary, "new": new_summary},
    }


def diff_pipeline_manifest(old: dict, new: dict) -> dict:
    """Targets are stably named (table_name), like quality-report's checks. The two things a
    team actually wants to know on a re-run are: did the modality choice change (a bigger deal
    than it sounds -- it means every generated file for that target is being replaced), and did
    readiness_level or the idempotency result regress (e.g. validated -> draft because the
    contract changed underneath the pipeline)."""
    old_targets = _index_by(old.get("targets", []), lambda t: t["table_name"])
    new_targets = _index_by(new.get("targets", []), lambda t: t["table_name"])

    added = sorted(new_targets.keys() - old_targets.keys())
    removed = sorted(old_targets.keys() - new_targets.keys())
    changed = []
    for name in sorted(old_targets.keys() & new_targets.keys()):
        o, n = old_targets[name], new_targets[name]
        target_changes = {}
        if o.get("load_pattern") != n.get("load_pattern"):
            target_changes["load_pattern"] = {"old": o.get("load_pattern"), "new": n.get("load_pattern")}
        if o.get("merge_keys") != n.get("merge_keys"):
            target_changes["merge_keys"] = {"old": o.get("merge_keys"), "new": n.get("merge_keys")}
        old_files = sorted(f["path"] for f in o.get("generated_files", []))
        new_files = sorted(f["path"] for f in n.get("generated_files", []))
        if old_files != new_files:
            target_changes["generated_files"] = {"old": old_files, "new": new_files}
        if target_changes:
            changed.append({"table_name": name, "changes": target_changes})

    old_modality = old.get("modality_decision", {}).get("chosen")
    new_modality = new.get("modality_decision", {}).get("chosen")

    return {
        "targets": {"added": added, "removed": removed, "changed": changed},
        "modality_decision.chosen": {"old": old_modality, "new": new_modality},
        "readiness_level": {"old": old.get("readiness_level"), "new": new.get("readiness_level")},
        "idempotency_check.result": {"old": old.get("idempotency_check", {}).get("result"),
                                      "new": new.get("idempotency_check", {}).get("result")},
    }


DIFFERS = {
    "data-contract": diff_data_contract,
    "validation-report": diff_validation_report,
    "quality-report": diff_quality_report,
    "pipeline-manifest": diff_pipeline_manifest,
}


def is_material(diff: dict) -> bool:
    """True if the diff contains any actual change (used for the 'nothing changed' short-circuit
    described in toolkit-conventions.md #8)."""
    def _any(node):
        if isinstance(node, dict):
            if set(node.keys()) == {"old", "new"}:
                return node["old"] != node["new"]
            return any(_any(v) for v in node.values())
        if isinstance(node, list):
            return len(node) > 0
        return False
    return _any(diff)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("old_artifact", type=Path)
    parser.add_argument("new_artifact", type=Path)
    parser.add_argument("--schema-type", required=True, choices=list(DIFFERS))
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    old = json.loads(args.old_artifact.read_text())
    new = json.loads(args.new_artifact.read_text())
    diff = DIFFERS[args.schema_type](old, new)
    result = {"schema_type": args.schema_type, "material_change": is_material(diff), "diff": diff}

    output = json.dumps(result, indent=2)
    if args.out:
        args.out.write_text(output)
    print(output)
    sys.exit(0)


if __name__ == "__main__":
    main()
