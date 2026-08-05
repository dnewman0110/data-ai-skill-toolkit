#!/usr/bin/env python3
"""
build_deployment_manifest.py -- the one command SKILL.md tells the agent to run. Wires together:
check_target_approval (the gate -- halts immediately on anything short of an explicit, named,
already-recorded approval on the source pipeline-manifest), resolve_connector_type (source_system
-> Lakeflow Connect connector shape), and render_bundle_resources (the actual Asset Bundle YAML),
and produces deployment_findings.json -- the deterministic half. The agent then reads this,
assembles the final deployment-manifest.json (adding the run-manifest envelope, echoing
approval_gate, folding unsupported_source_systems/skipped targets into the artifact), and
validates it with scripts/validate_artifact.py before declaring success -- same split as
data-pipeline's build_pipeline_manifest.py / pipeline_findings.json.

This script never sets readiness_level to approved_for_deployment or deployed, and never writes a
non-null `deployment` object -- that requires a SECOND, separate, explicit human approval (distinct
from the pipeline-manifest approval this script's gate already required just to run at all), which
by definition cannot come from a script. See references/approval-gate.md and
toolkit-conventions.md #1 and #7. This script also never runs `databricks bundle deploy`, never
calls any Databricks API, and never creates a live connector -- it only writes YAML files under
output_dir, the same boundary data-pipeline's own code generation already draws.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_target_approval import check_target_approval, filter_targets_by_approval  # noqa: E402
from resolve_connector_type import resolve_connector_type  # noqa: E402
from render_bundle_resources import render_bundle_resources  # noqa: E402


def build_deployment_findings(pipeline_manifest: dict, pipeline_output_dir: Path,
                               source_system: str, connection_name: str, output_dir: Path) -> dict:
    gate = check_target_approval(pipeline_manifest)
    if gate["halted"]:
        return {"halted": True, "reason": gate["reason"], "targets": None}

    selection = filter_targets_by_approval(pipeline_manifest.get("targets", []), gate["target_named"])
    if selection["halted"]:
        return {"halted": True, "reason": selection["reason"], "targets": None}

    target = selection["processed_target"]
    transform_spec_path = pipeline_output_dir / target["transform_spec_ref"]
    if not transform_spec_path.exists():
        return {"halted": True, "reason": (
            f"transform_spec_ref '{target['transform_spec_ref']}' does not exist under "
            f"pipeline_output_dir '{pipeline_output_dir}' -- cannot determine this target's real "
            "source object without it."
        ), "targets": None}
    transform_spec = json.loads(transform_spec_path.read_text())

    try:
        connector_info = resolve_connector_type(source_system)
    except ValueError as e:
        return {
            "halted": False,
            "approval_gate": gate,
            "processed_target": None,
            "skipped_targets": selection["skipped_targets"],
            "unsupported_source_systems": [{
                "table_name": target["table_name"], "source_system_named": source_system, "reason": str(e),
            }],
        }

    codegen = render_bundle_resources(
        table_name=target["table_name"],
        target_catalog=target["target_catalog"],
        target_schema=target["target_schema"],
        source_schema=transform_spec["source_schema"],
        source_table=transform_spec["source_table"],
        merge_keys=target.get("merge_keys", []),
        connector_info=connector_info,
        connection_name=connection_name,
        output_dir=output_dir,
    )

    return {
        "halted": False,
        "approval_gate": gate,
        "processed_target": {
            "table_name": target["table_name"],
            "skipped": False,
            "skipped_reason": None,
            "source_system": connector_info["source_system"],
            "connector_type": connector_info["connector_type"],
            "connection_name": connection_name,
            "source_object": {"schema": transform_spec["source_schema"], "table": transform_spec["source_table"]},
            "destination": {"catalog": target["target_catalog"], "schema": target["target_schema"], "table": target["table_name"]},
            "merge_keys": target.get("merge_keys", []),
            "generated_files": codegen["generated_files"],
        },
        "skipped_targets": selection["skipped_targets"],
        "unsupported_source_systems": [],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pipeline-manifest-json", type=Path, required=True)
    parser.add_argument("--pipeline-output-dir", type=Path, required=True,
                         help="output_dir the source pipeline-manifest's transform_spec_ref paths are relative to.")
    parser.add_argument("--source-system", required=True,
                         help="Named by the invoking agent per SKILL.md step 2 -- never parsed from the contract.")
    parser.add_argument("--connection-name", required=True,
                         help="Name of the Unity Catalog connection this target's ingestion pipeline depends on.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    pipeline_manifest = json.loads(args.pipeline_manifest_json.read_text())
    result = build_deployment_findings(
        pipeline_manifest, args.pipeline_output_dir, args.source_system, args.connection_name, args.output_dir,
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
