#!/usr/bin/env python3
"""
run_assertions.py -- deterministic checks for data-deploy, runnable in CI with no subagent and no
LLM call. Covers: this skill never deploys/executes anything (a static scan, since data-deploy has
no lakehouse adapter at all -- it's pure manifest transformation and template rendering, so "never
deploys" is checkable as "never invokes a process or network call", stronger and easier to prove
than the other skills' "no DDL/DML" scan); malformed/unsupported-major artifacts rejected; the
approval gate refuses every way it should (wrong modality, not approved, target_named naming a
table that doesn't exist); a target not named in the approval is skipped, never silently included;
an unsupported source_system is reported, never guessed at; and a live end-to-end run for TWO
different connector types (Salesforce, SQL Server) actually renders correct, parseable bundle
resources -- proving "generic across source systems" rather than just asserting it.

Exit 0 if every check passes, 1 otherwise (prints every failure).
"""
import json
import re
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = Path(__file__).resolve().parents[1]
FIXTURES_DIR = SKILL_DIR / "evals" / "fixtures"
PIPELINE_OUTPUT_DIR = FIXTURES_DIR / "pipeline_output"

sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from check_target_approval import check_target_approval, filter_targets_by_approval  # noqa: E402
from resolve_connector_type import resolve_connector_type, CONNECTOR_TYPES  # noqa: E402
from build_deployment_manifest import build_deployment_findings  # noqa: E402

failures = []


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


def load_fixture(name):
    return json.loads((FIXTURES_DIR / name).read_text())


# -- 1. This skill never deploys, schedules, or executes anything -- static scan. Unlike the
#    read-only skills' "no DDL/DML" check, data-deploy's promise is stronger: it should have no
#    ability to invoke a process, hit a network endpoint, or call any Databricks API at all, since
#    its entire job is rendering YAML text to output_dir.
forbidden_patterns = re.compile(
    r"\b(subprocess|os\.system|os\.popen|requests\.|urllib\.request|httpx\.|socket\.|databricks_cli|WorkspaceClient)\b"
)
scripts_dir = SKILL_DIR / "scripts"
offending = []
for py_file in scripts_dir.glob("*.py"):
    text = py_file.read_text()
    for m in forbidden_patterns.finditer(text):
        offending.append(f"{py_file.name}: found '{m.group(0)}'")
check("data-deploy scripts contain no process/network/Databricks-API invocation", len(offending) == 0)
for o in offending:
    print(f"    {o}")

# -- 2. Malformed / unsupported-major artifact rejected cleanly, not best-effort parsed.
example_path = REPO_ROOT / "contracts" / "examples" / "deployment-manifest.example.json"
if example_path.exists():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import subprocess as _subprocess  # local import: only this eval script (not any data-deploy
                                       # script) needs a process to invoke validate_artifact.py's CLI
    result = _subprocess.run(
        [sys.executable, "scripts/validate_artifact.py", str(example_path),
         "--schema-type", "deployment-manifest", "--supported-major", "99"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    output_text = (result.stdout + result.stderr).lower()
    check("Deployment-manifest with unsupported major version is refused (nonzero exit, clear message)",
          result.returncode != 0 and ("unsupported" in output_text or "supports major version" in output_text))

    result = _subprocess.run(
        [sys.executable, "scripts/validate_artifact.py", str(example_path),
         "--schema-type", "deployment-manifest", "--supported-major", "1"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    check("The shipped deployment-manifest.example.json is itself schema-valid",
          result.returncode == 0)

bad_artifact = {"schema_version": "1.0.0", "run": {}, "deployment_id": "x"}
bad_path = Path(tempfile.gettempdir()) / "bad_deployment_manifest.json"
bad_path.write_text(json.dumps(bad_artifact))
result = _subprocess.run(
    [sys.executable, "scripts/validate_artifact.py", str(bad_path), "--schema-type", "deployment-manifest"],
    cwd=REPO_ROOT, capture_output=True, text=True,
)
check("Structurally invalid deployment-manifest (missing required fields) is rejected", result.returncode != 0)

# -- 3. Connector-type resolution: known systems resolve, unknown systems are refused, never guessed.
check("resolve_connector_type('sql_server') resolves to SQLSERVER",
      resolve_connector_type("sql_server")["connector_type"] == "SQLSERVER")
check("resolve_connector_type('Salesforce') is case/whitespace-insensitive and resolves to SALESFORCE",
      resolve_connector_type("Salesforce")["connector_type"] == "SALESFORCE")
try:
    resolve_connector_type("sap")
    check("resolve_connector_type('sap') (unsupported) raises rather than guessing", False)
except ValueError as e:
    check("resolve_connector_type('sap') (unsupported) raises rather than guessing", "Unrecognized source_system" in str(e))
check("At least 5 source systems are supported (SQL Server, Salesforce, ServiceNow, Workday, SharePoint)",
      len(CONNECTOR_TYPES) >= 5)

# -- 4. Approval gate: every refusal path.
wrong_modality = load_fixture("wrong-modality-pipeline-manifest.json")
gate = check_target_approval(wrong_modality)
check("A non-lakeflow_connect modality halts before anything else runs",
      gate["halted"] and "lakeflow_connect" in gate["reason"])

not_approved = load_fixture("not-approved-pipeline-manifest.json")
gate = check_target_approval(not_approved)
check("A pipeline-manifest not yet approved_for_deployment halts",
      gate["halted"] and "approved_for_deployment" in gate["reason"])

mismatch = load_fixture("target-named-mismatch-pipeline-manifest.json")
gate = check_target_approval(mismatch)
check("Gate passes when deployment.approved is true and target_named is set (mismatch is a separate, later check)",
      not gate["halted"])
selection = filter_targets_by_approval(mismatch["targets"], gate["target_named"])
check("deployment.target_named naming a table absent from targets[] halts rather than guessing",
      selection["halted"] and "nonexistent_table" in selection["reason"])

# -- 5. Multi-target: only the approved target is processed, the other is skipped with a reason,
#    never silently dropped.
multi = load_fixture("multi-target-pipeline-manifest.json")
with tempfile.TemporaryDirectory() as tmp:
    result = build_deployment_findings(
        multi, PIPELINE_OUTPUT_DIR, source_system="salesforce",
        connection_name="acme_salesforce_prod_connection", output_dir=Path(tmp),
    )
    check("Multi-target manifest: not halted", not result["halted"])
    check("Multi-target manifest: the approved target is processed",
          result["processed_target"] is not None
          and result["processed_target"]["table_name"] == "bronze_salesforce_opportunity")
    check("Multi-target manifest: the unnamed target is recorded skipped with a reason, not dropped",
          len(result["skipped_targets"]) == 1
          and result["skipped_targets"][0]["table_name"] == "dim_customer_workday"
          and "not named" in result["skipped_targets"][0]["skipped_reason"])

# -- 6. Unsupported source_system is reported, never guessed at a fallback shape.
salesforce_manifest = load_fixture("salesforce-approved-pipeline-manifest.json")
with tempfile.TemporaryDirectory() as tmp:
    result = build_deployment_findings(
        salesforce_manifest, PIPELINE_OUTPUT_DIR, source_system="sap",
        connection_name="whatever", output_dir=Path(tmp),
    )
    check("An unsupported source_system does not halt the whole run",
          not result["halted"])
    check("An unsupported source_system is reported in unsupported_source_systems, not silently guessed",
          len(result["unsupported_source_systems"]) == 1
          and result["unsupported_source_systems"][0]["source_system_named"] == "sap")
    check("No bundle resources are generated for an unsupported source_system",
          result["processed_target"] is None)

# -- 7. Live end-to-end runs for TWO different connector types -- "generic across source systems"
#    actually exercised, not just asserted. Real files rendered to a real temp output_dir, then
#    parsed back as YAML to confirm they're structurally valid, not just non-empty strings.
CONNECTOR_CASES = [
    ("salesforce-approved-pipeline-manifest.json", "bronze_salesforce_opportunity", "salesforce", "SALESFORCE",
     {"schema": "crm", "table": "opportunity"}),
    ("sqlserver-approved-pipeline-manifest.json", "bronze_sqlserver_product", "sql_server", "SQLSERVER",
     {"schema": "SalesLT", "table": "Product"}),
]

for fixture_name, table_name, source_system, expected_connector_type, expected_source in CONNECTOR_CASES:
    manifest = load_fixture(fixture_name)
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        result = build_deployment_findings(
            manifest, PIPELINE_OUTPUT_DIR, source_system=source_system,
            connection_name=f"acme_{source_system}_prod_connection", output_dir=out_dir,
        )
        check(f"[{source_system}] end-to-end run is not halted", not result["halted"])
        target = result.get("processed_target") or {}
        check(f"[{source_system}] resolves to connector_type {expected_connector_type}",
              target.get("connector_type") == expected_connector_type)
        check(f"[{source_system}] source_object matches the fixture's transform_spec.json",
              target.get("source_object") == expected_source)

        connection_path = out_dir / "generated" / table_name / "uc_connection.yml"
        pipeline_path = out_dir / "generated" / table_name / "ingestion_pipeline.yml"
        check(f"[{source_system}] uc_connection.yml was written to disk", connection_path.exists())
        check(f"[{source_system}] ingestion_pipeline.yml was written to disk", pipeline_path.exists())

        if yaml is not None and connection_path.exists() and pipeline_path.exists():
            connection_doc = yaml.safe_load(connection_path.read_text())
            pipeline_doc = yaml.safe_load(pipeline_path.read_text())
            check(f"[{source_system}] uc_connection.yml parses as valid YAML with the right connection_type",
                  connection_doc["connection"]["connection_type"] == expected_connector_type)
            ingestion_objects = pipeline_doc["resources"]["pipelines"][f"{table_name}_ingestion"]["ingestion_definition"]["objects"]
            rendered_source = ingestion_objects[0]["table"]
            check(f"[{source_system}] ingestion_pipeline.yml parses as valid YAML with the right source object",
                  rendered_source["source_schema"] == expected_source["schema"]
                  and rendered_source["source_table"] == expected_source["table"])
            check(f"[{source_system}] ingestion_pipeline.yml's destination matches the target's catalog/schema/table",
                  rendered_source["destination_catalog"] == manifest["targets"][0]["target_catalog"]
                  and rendered_source["destination_schema"] == manifest["targets"][0]["target_schema"]
                  and rendered_source["destination_table"] == table_name)
            merge_keys = manifest["targets"][0]["merge_keys"]
            if merge_keys:
                check(f"[{source_system}] merge_keys render as table_configuration.primary_keys",
                      rendered_source.get("table_configuration", {}).get("primary_keys") == merge_keys)
        elif yaml is None:
            print("    [SKIP] pyyaml not installed -- rendered-YAML structural checks skipped (files still asserted present above)")

print()
if failures:
    print(f"FAIL: {len(failures)} assertion(s) failed.")
    sys.exit(1)
else:
    print("PASS: all deterministic assertions passed.")
    sys.exit(0)
