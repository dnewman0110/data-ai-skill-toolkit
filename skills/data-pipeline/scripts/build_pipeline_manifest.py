#!/usr/bin/env python3
"""
build_pipeline_manifest.py -- the one command SKILL.md tells the agent to run for a target table.
Wires together: build_transform_spec (derive the portable logic from a data-contract),
derive_mock_data (synthesize a source dataset), validate_pipeline_locally (idempotency proof),
generate_pipeline_code (render the chosen modality's files), and produces
pipeline_findings.json -- the deterministic half. The agent then reads this, classifies the
modality rubric_factors that genuinely require judgment (this script does NOT call
recommend_modality.py itself -- see SKILL.md step 2, and references/decision-rubric.md for why
that classification step belongs to the agent, not this orchestrator), assembles the final
pipeline-manifest.json, and validates it with scripts/validate_artifact.py before declaring
success.

This script never sets readiness_level to approved_for_deployment or deployed, and never writes a
non-null `deployment` object -- those require an explicit, in-conversation human approval naming
the target, which by definition cannot come from a script. See
references/toolkit-conventions.md #1 and #7.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_transform_spec import build_transform_spec  # noqa: E402
from derive_mock_data import derive_mock_data  # noqa: E402
from validate_pipeline_locally import validate_pipeline_locally  # noqa: E402
from generate_pipeline_code import generate_pipeline_code  # noqa: E402


def build_pipeline_findings(contract: dict, table_name: str, modality: str,
                             output_dir: Path, mock_row_count: int = 50, mock_seed: int = 1337,
                             sensitive_columns: list[dict] = None,
                             pii_target_transform: dict = None) -> dict:
    table = next((t for t in contract["tables"] if t["name"] == table_name), None)
    if table is None:
        return {"halted": True, "reason": f"No table '{table_name}' in this data-contract.", "targets": None}

    try:
        spec = build_transform_spec(contract, table_name, sensitive_columns=sensitive_columns,
                                     pii_target_transform=pii_target_transform)
    except ValueError as e:
        return {"halted": True, "reason": str(e), "targets": None}

    mock_rows = derive_mock_data(table, row_count=mock_row_count, seed=mock_seed)
    mock_dir = output_dir / "mock_data"
    mock_dir.mkdir(parents=True, exist_ok=True)
    # Filename is qualified by TARGET table, not just source table -- mock data content depends on
    # the target's declared types/nullability/tests (see derive_mock_data.py's docstring), so two
    # targets sharing one source object (e.g. a fact and a dimension both sourced from
    # silver.orders) produce DIFFERENT mock content and must not silently overwrite each other's
    # file, which is what a source-table-only filename did.
    mock_path = mock_dir / f"{table_name}__{spec['source_schema']}.{spec['source_table']}.json"
    mock_path.write_text(json.dumps(mock_rows, indent=2))

    idempotency = validate_pipeline_locally(spec, mock_rows)
    evidence_path = output_dir / "generated" / table_name / "idempotency_evidence.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(idempotency, indent=2))

    if modality == "lakeflow_connect":
        # Idempotency for a managed connector is the connector's own concern, not something this
        # toolkit tests locally -- see contracts/pipeline-manifest.schema.json's idempotency_check.
        idempotency_result_for_manifest = "not_applicable"
    else:
        idempotency_result_for_manifest = idempotency["result"]

    codegen = generate_pipeline_code(spec, modality, output_dir)

    return {
        "halted": False,
        "transform_spec": spec,
        "target": {
            "table_name": table_name,
            "target_catalog": spec["target_catalog"],
            "target_schema": spec["target_schema"],
            "load_pattern": spec["load_pattern"],
            "merge_keys": spec["merge_keys"],
            "generated_files": codegen["generated_files"],
            "transform_spec_ref": codegen["transform_spec_ref"],
            "tests_carried_forward": codegen["tests_carried_forward"],
        },
        "mock_data": {
            "generated": True,
            "location": str(mock_dir),
            # Keyed by target table too, for the same reason the filename is -- see above.
            "row_counts_by_table": {f"{table_name} <- {spec['source_schema']}.{spec['source_table']}": len(mock_rows)},
        },
        "idempotency_check": {
            "performed": idempotency["performed"],
            "method": idempotency["method"],
            "result": idempotency_result_for_manifest,
            "evidence_ref": str(evidence_path),
        },
        "low_confidence_mappings": spec["low_confidence_mappings"],
        # Config gaps (no target-transform rule defined) plus modality-capability gaps (Lakeflow
        # Connect can't transform columns at all) -- see build_transform_spec.py and
        # generate_pipeline_code.py's _pii_transform_notes. Never silently dropped: SKILL.md step 4
        # folds this into the final manifest's assumptions[], same treatment as low_confidence_mappings.
        "pii_transform_gaps": spec["target_transform_gaps"] + codegen["pii_transform_notes"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--contract-json", type=Path, required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--modality", required=True, choices=["pyspark_notebook", "declarative_pipeline", "lakeflow_connect"],
                         help="The modality already chosen (e.g. via recommend_modality.py, run by the agent per SKILL.md step 2) -- this script does not choose it.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mock-row-count", type=int, default=50)
    parser.add_argument("--mock-seed", type=int, default=1337)
    parser.add_argument("--sensitive-columns-json", type=Path, default=None,
                         help="JSON file: toolkit.yaml's sample_data.sensitive_columns list")
    parser.add_argument("--pii-target-transform-json", type=Path, default=None,
                         help="JSON file: toolkit.yaml's pii_handling.target_transform object")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    contract = json.loads(args.contract_json.read_text())
    sensitive_columns = (json.loads(args.sensitive_columns_json.read_text())
                         if args.sensitive_columns_json else [])
    pii_target_transform = (json.loads(args.pii_target_transform_json.read_text())
                            if args.pii_target_transform_json else {})
    result = build_pipeline_findings(
        contract, args.table, args.modality, args.output_dir,
        mock_row_count=args.mock_row_count, mock_seed=args.mock_seed,
        sensitive_columns=sensitive_columns, pii_target_transform=pii_target_transform,
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
