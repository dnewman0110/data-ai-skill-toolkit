#!/usr/bin/env python3
"""
check_target_approval.py -- the one gate every data-deploy run passes through before generating
anything. Enforces the precondition toolkit-conventions.md #1/#7 and references/approval-gate.md
require: a pipeline-manifest.json is only eligible for this skill at all when its OWN recorded
human approval (deployment.approved, set by data-pipeline per its SKILL.md step 7 -- never by a
script) names a specific target, its modality is lakeflow_connect (the only modality this skill
knows how to turn into bundle resources), and its readiness_level has reached
approved_for_deployment. Anything short of that halts here, before build_deployment_manifest.py
does anything else -- this script never proceeds on a partial or inferred approval.

Separately, filter_targets_by_approval enforces "refuse to touch any target not named": only the
ONE target table matching deployment.target_named is processed; every other target in the source
manifest is recorded as skipped, never silently dropped or silently included.
"""
import argparse
import json
import sys


def check_target_approval(pipeline_manifest: dict) -> dict:
    modality = pipeline_manifest.get("modality_decision", {}).get("chosen")
    if modality != "lakeflow_connect":
        return {"halted": True, "reason": (
            f"pipeline-manifest.modality_decision.chosen is '{modality}', not 'lakeflow_connect' -- "
            "data-deploy only turns Lakeflow Connect pipeline manifests into bundle resources. "
            "A different modality's generated code deploys through whatever mechanism data-pipeline's "
            "own SKILL.md step 7 and toolkit-conventions.md #7 gate 3 already describe for it."
        )}

    readiness = pipeline_manifest.get("readiness_level")
    if readiness != "approved_for_deployment":
        return {"halted": True, "reason": (
            f"pipeline-manifest.readiness_level is '{readiness}', not 'approved_for_deployment'. "
            "data-deploy will not generate bundle resources for a pipeline manifest a human has not "
            "yet explicitly approved -- see references/approval-gate.md."
        )}

    deployment = pipeline_manifest.get("deployment")
    if not deployment or deployment.get("approved") is not True:
        return {"halted": True, "reason": (
            "pipeline-manifest.deployment.approved is not true. readiness_level claims "
            "approved_for_deployment but the deployment object itself is missing or not approved -- "
            "refusing to proceed on an inconsistent artifact rather than trusting readiness_level alone."
        )}

    target_named = deployment.get("target_named")
    if not target_named:
        return {"halted": True, "reason": (
            "pipeline-manifest.deployment.target_named is empty. A blanket approval with no named "
            "target does not satisfy this skill's gate -- see references/approval-gate.md."
        )}

    return {
        "halted": False,
        "reason": None,
        "target_named": target_named,
        "approved_by": deployment.get("approved_by"),
        "approved_at": deployment.get("approved_at"),
    }


def filter_targets_by_approval(targets: list, target_named: str) -> dict:
    matching = [t for t in targets if t.get("table_name") == target_named]
    if not matching:
        available = ", ".join(sorted(t.get("table_name", "?") for t in targets))
        return {"halted": True, "reason": (
            f"deployment.target_named '{target_named}' does not match any target_name in this "
            f"pipeline-manifest's targets[] (available: {available}). Refusing to guess which "
            "target the approval meant."
        )}

    processed = matching[0]
    skipped = [
        {"table_name": t["table_name"], "skipped": True,
         "skipped_reason": f"not named in deployment.target_named ('{target_named}')"}
        for t in targets if t.get("table_name") != target_named
    ]
    return {"halted": False, "reason": None, "processed_target": processed, "skipped_targets": skipped}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pipeline-manifest-json", required=True, help="Path to pipeline-manifest.json")
    args = parser.parse_args()

    with open(args.pipeline_manifest_json) as f:
        manifest = json.load(f)

    gate = check_target_approval(manifest)
    if gate["halted"]:
        print(json.dumps(gate, indent=2))
        sys.exit(1)

    selection = filter_targets_by_approval(manifest.get("targets", []), gate["target_named"])
    result = {**gate, **selection}
    print(json.dumps(result, indent=2))
    sys.exit(1 if selection["halted"] else 0)


if __name__ == "__main__":
    main()
