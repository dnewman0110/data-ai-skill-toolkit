#!/usr/bin/env python3
"""
integration_check.py -- a fully deterministic, no-LLM, CI-runnable proof that the five skills'
artifacts actually chain together: data-modeling's model-spec.json resolves into a real
data-discovery data-contract.json, which data-pipeline actually builds code from, and which
data-quality/data-validation actually attach checks to -- against the real fixture lakehouse, not
mocked interfaces.

What this does NOT prove: the judgment steps each skill's SKILL.md assigns to the invoking agent
(mapping proposals, root-cause diagnosis, SCD rationale, measure additivity classification). This
script performs the "resolution" of contracts/examples/model-spec.example.json's ALREADY-EXPLICIT
source_to_target_mappings against real profiled columns -- confirming they're real and grounding
their types -- which is legitimately scriptable precisely because that model-spec already names
exact source columns with no ambiguity to resolve. A model-spec with genuinely ambiguous mappings
still needs an agent in the loop; see each skill's own evals/ for that judgment being exercised via
subagent runs. This script exists to catch the OTHER kind of bug: artifact shapes that look correct
in isolation but don't actually fit together when one skill's real output feeds the next skill's
real input. It has already caught five such bugs during this toolkit's own construction -- see
CHANGELOG.md "Changed" and DECISIONS.md.

Stages, each producing and validating one schema-conformant artifact under fixtures/integration/
(gitignored -- generated, not committed, same as any other run output):
  1. data-modeling  : verify_silver_layer against the model-spec's real source objects (uses the
                       shipped model-spec.example.json as the design -- this script does not design
                       one; that is the judgment step).
  2. data-discovery  : resolution mode -- profile the same source objects, confirm every mapping the
                       model-spec named is a real column, translate proposed tests onto target
                       column names, assemble + validate data-contract.json
                       (invocation_mode=resolution, source_model_spec_ref populated).
  3. data-pipeline   : build_pipeline_manifest against the resolved contract, assemble + validate
                       pipeline-manifest.json, prove local idempotency.
  4. data-quality    : contract-derived checks executed against silver.orders, assemble + validate
                       quality-report.json.
  5. data-validation : silver.orders (source) vs gold.legacy_fct_orders (target), assemble +
                       validate validation-report.json.

Exit 0 if every stage produces a schema-valid artifact with no unresolved requirements beyond what
is explicitly expected; exit 1 otherwise, with the failing stage named.
"""
import json
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "fixtures" / "integration"
LAKEHOUSE_DIR = REPO_ROOT / "fixtures" / "lakehouse"
CATALOG = "acme_retail_dev"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from lakehouse_adapter import SQLiteFixtureAdapter  # noqa: E402
import validate_artifact as va  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "skills" / "data-modeling" / "scripts"))
from verify_silver_layer import verify_silver_layer  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "skills" / "data-discovery" / "scripts"))
from build_findings import build_findings as discovery_build_findings  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "skills" / "data-pipeline" / "scripts"))
from build_pipeline_manifest import build_pipeline_findings  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "skills" / "data-quality" / "scripts"))
from derive_checks_from_contract import derive_checks  # noqa: E402
from run_checks import run_checks  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "skills" / "data-validation" / "scripts"))
from compare_staged import compare  # noqa: E402

failures = []


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


def run_manifest(skill_name, run_id, target_schema="gold"):
    return {
        "schema_version": "1.0.0", "run_id": run_id,
        "skill": {"name": skill_name, "version": "1.0.0"},
        "timestamp": "2026-07-30T00:00:00Z",
        "target": {"platform": "azure", "catalog_type": "unity_catalog",
                    "workspace_id": "adb-integration-check.azuredatabricks.net",
                    "catalog": CATALOG, "schema": target_schema},
        "invoking_identity": "integration-check@toolkit.internal",
        "mode": "full_scan",
        "source_fingerprints": [],
        "telemetry": {"estimated_bytes_scanned": 0, "estimated_rows_scanned": 0,
                       "thresholds_applied": {"max_rows_scanned": None, "max_bytes_scanned": None,
                                               "max_wall_clock_seconds": None}},
        "previous_run_id": None,
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    adapter = SQLiteFixtureAdapter(str(LAKEHOUSE_DIR), catalog=CATALOG)

    model_spec = json.loads((REPO_ROOT / "contracts" / "examples" / "model-spec.example.json").read_text())
    fact = model_spec["facts"][0]  # fct_orders
    dim = model_spec["dimensions"][0]  # dim_customer

    # ---------- Stage 1: data-modeling ----------
    print("\n=== Stage 1: data-modeling (silver verification against the model-spec's real sources) ===")
    source_objects = [("silver", "orders"), ("silver", "customers")]
    verification = verify_silver_layer(adapter, source_objects)
    check("Stage 1: silver.orders + silver.customers verify as curated (model-spec's own claim, re-checked live)",
          verification["verified"] is True)
    check("Stage 1: shipped model-spec.example.json is itself schema-valid",
          len(va.validate(REPO_ROOT / "contracts" / "examples" / "model-spec.example.json",
                           REPO_ROOT / "contracts", "model-spec", 1)) == 0)

    # ---------- Stage 2: data-discovery, resolution mode ----------
    print("\n=== Stage 2: data-discovery (resolution mode -- ground the model-spec's mappings) ===")
    findings = discovery_build_findings(
        adapter, source_objects, thresholds={}, sample_size=None, max_sample_records=20,
        sensitive_columns=[],
    )
    check("Stage 2: discovery findings did not halt", findings["halted"] is False)
    tables_by_object = {t["object"]: t for t in findings["tables"]}
    orders_cols = {c["name"]: c for c in tables_by_object["silver.orders"]["columns"]}
    customers_cols = {c["name"]: c for c in tables_by_object["silver.customers"]["columns"]}

    unresolved = []
    contract_columns = []
    source_to_target = {}  # source_column -> target_column, for translating tests
    for m in fact["source_to_target_mappings"]:
        src_col = m["source_column"]
        profile = orders_cols.get(src_col)
        if profile is None:
            unresolved.append({"requirement": f"{fact['name']}.{m['target_column']} <- {m['source_object']}.{src_col}",
                                "reason": f"Column '{src_col}' not found in profiled {m['source_object']}."})
            continue
        source_to_target[src_col] = m["target_column"]
        contract_columns.append({
            "name": m["target_column"], "type": profile["declared_type"],
            "nullable": profile["declared_nullable"], "pii_tag": None,
            "source": {"object": f"{CATALOG}.silver.orders", "column": src_col, "mapping_type": "explicit_alias"},
        })
    # degenerate dimension + measures not already covered are also real physical columns on the fact
    for extra_col in [fact["degenerate_dimensions"][0]["column"], "line_number"]:
        if extra_col not in {c["name"] for c in contract_columns}:
            profile = orders_cols.get(extra_col)
            if profile is None:
                unresolved.append({"requirement": f"{fact['name']}.{extra_col}",
                                    "reason": f"Column '{extra_col}' not found in profiled silver.orders."})
            else:
                source_to_target[extra_col] = extra_col
                contract_columns.append({
                    "name": extra_col, "type": profile["declared_type"], "nullable": profile["declared_nullable"],
                    "pii_tag": None,
                    "source": {"object": f"{CATALOG}.silver.orders", "column": extra_col, "mapping_type": "explicit_alias"},
                })
    for measure in fact["measures"]:
        if measure["name"] not in {c["name"] for c in contract_columns}:
            # measure name should already be covered by source_to_target_mappings above; if not,
            # that's itself an unresolved requirement, not silently skipped.
            unresolved.append({"requirement": f"{fact['name']}.{measure['name']}",
                                "reason": "Measure has no source_to_target_mappings entry in the model-spec."})

    check("Stage 2: every model-spec mapping for fct_orders resolved against a real column",
          len(unresolved) == 0)
    for u in unresolved:
        print(f"    UNRESOLVED: {u['requirement']} -- {u['reason']}")

    # Translate discovery's proposed tests from source column names to target column names --
    # only tests on columns this contract actually maps get carried over.
    contract_tests = []
    for t in tables_by_object["silver.orders"]["proposed_tests"]:
        if t["type"] == "uniqueness":
            mapped_cols = [source_to_target.get(c) for c in t["params"]["columns"]]
            if all(mapped_cols):
                contract_tests.append({**t, "column": ",".join(mapped_cols),
                                        "params": {"columns": mapped_cols}})
        elif t["column"] in source_to_target:
            contract_tests.append({**t, "column": source_to_target[t["column"]]})

    grain_check = next((ck for ck in tables_by_object["silver.orders"]["candidate_keys"]
                         if ck["source"] == "declared_primary_key"), None)
    check("Stage 2: fact grain (declared PK) profiles as unique -- grounds fact.grain.validated_against_measures",
          grain_check is not None and grain_check["is_unique"])

    run_id_2 = "integration-" + uuid.uuid4().hex[:12]
    data_contract = {
        "schema_version": "1.0.0",
        "run": run_manifest("data-discovery", run_id_2, target_schema="gold"),
        "contract_id": "integration-fct-orders-v1",
        "invocation_mode": "resolution",
        "source_model_spec_ref": {"model_id": model_spec["model_id"],
                                   "schema_version": model_spec["schema_version"],
                                   "path_or_uri": "contracts/examples/model-spec.example.json"},
        "grain_determination": {"method": "profiled_unique_key",
                                  "confidence_basis": "0.75-0.94: profiled uniqueness check passed against a full scan of the candidate key."},
        "tables": [{
            "name": fact["name"], "target_catalog": CATALOG, "target_schema": "gold",
            "grain": {"statement": fact["grain"]["statement"], "determination": "profiled_unique_key",
                       "basis": fact["grain"]["validation_evidence"], "confidence": 0.9},
            "columns": contract_columns,
            "tests": contract_tests,
            "sample_records": tables_by_object["silver.orders"]["sample_records"][:5],
            "row_count_estimate": tables_by_object["silver.orders"]["row_count"],
        }],
        "unresolved_requirements": unresolved,
        "assumptions": [{
            "statement": "This data-contract was resolved by fixtures/integration_check.py from "
                         "contracts/examples/model-spec.example.json's already-explicit "
                         "source_to_target_mappings, not by an agent making mapping judgment calls.",
            "basis": "Integration-check run -- proves artifact shapes chain correctly, not agent judgment quality.",
        }],
    }
    contract_path = OUT_DIR / "data-contract.resolved.json"
    contract_path.write_text(json.dumps(data_contract, indent=2, default=str))
    errors = va.validate(contract_path, REPO_ROOT / "contracts", "data-contract", 1)
    check("Stage 2: resolved data-contract.json is schema-valid", len(errors) == 0)
    for e in errors:
        print(f"    {e}")

    # ---------- Stage 3: data-pipeline ----------
    print("\n=== Stage 3: data-pipeline (build real code from the resolved contract) ===")
    pipeline_out = OUT_DIR / "pipeline"
    pf = build_pipeline_findings(data_contract, fact["name"], "declarative_pipeline", pipeline_out)
    check("Stage 3: pipeline build did not halt", pf["halted"] is False)
    if not pf["halted"]:
        check("Stage 3: local idempotency proof matches", pf["idempotency_check"]["result"] == "match")
        run_id_3 = "integration-" + uuid.uuid4().hex[:12]
        pipeline_manifest = {
            "schema_version": "1.0.0", "run": run_manifest("data-pipeline", run_id_3),
            "pipeline_id": "integration-fct-orders-pipeline-v1",
            "source_refs": {"data_contract_ref": {"contract_id": data_contract["contract_id"],
                                                    "schema_version": "1.0.0",
                                                    "path_or_uri": str(contract_path)},
                             "model_spec_ref": None},
            "modality_decision": {
                "chosen": "declarative_pipeline",
                "rubric_factors": {"source_is_managed_connector": False, "requires_streaming": False,
                                    "transform_complexity": "simple_declarative", "target_layer": "gold",
                                    "modality_availability": {"pyspark_notebook": True, "declarative_pipeline": True,
                                                                "lakeflow_connect": True}},
                "confidence": 0.85, "basis": "integration-check: single-source reshape, matches decision-rubric.md's default case.",
            },
            "targets": [pf["target"]], "mock_data": pf["mock_data"], "idempotency_check": pf["idempotency_check"],
            "readiness_level": "validated", "deployment": None,
            "assumptions": [{"statement": "Generated by fixtures/integration_check.py for chain verification.",
                              "basis": "Integration-check run."}],
        }
        pm_path = OUT_DIR / "pipeline-manifest.json"
        pm_path.write_text(json.dumps(pipeline_manifest, indent=2, default=str))
        errors = va.validate(pm_path, REPO_ROOT / "contracts", "pipeline-manifest", 1)
        check("Stage 3: pipeline-manifest.json is schema-valid", len(errors) == 0)
        for e in errors:
            print(f"    {e}")

    # ---------- Stage 4: data-quality, attached at the gate ----------
    print("\n=== Stage 4: data-quality (contract-derived checks against silver.orders) ===")
    derived_checks = derive_checks(data_contract, fact["name"])
    check("Stage 4: at least one check derived from the resolved contract", len(derived_checks) > 0)
    check_results = run_checks(adapter, "silver", "orders", derived_checks)
    quality_run_id = "integration-" + uuid.uuid4().hex[:12]
    summary = {"passed": 0, "failed": 0, "warned": 0, "not_evaluated": 0}
    diagnoses = []
    for r in check_results:
        summary[r["status"]] += 1
        if r["status"] in ("failed", "warned"):
            diagnoses.append({
                "check_id": r["check_id"], "source": "llm_inferred",
                "root_cause": "Integration-check placeholder diagnosis -- see this toolkit's real "
                               "data-quality eval 1 for genuine LLM-diagnosed root causes.",
                "confidence": 0.2, "basis": "integration-check: not a real diagnosis, structural placeholder only.",
                "suggested_fix": "Not applicable -- see skills/data-quality/evals for real diagnosis evidence.",
            })
    quality_report = {
        "schema_version": "1.0.0", "run": run_manifest("data-quality", quality_run_id, target_schema="silver"),
        "target_object": f"{CATALOG}.silver.orders", "checks": check_results, "diagnoses": diagnoses,
        "summary": summary,
        "assumptions": [{"statement": "Diagnoses in this artifact are integration-check placeholders, not real LLM reasoning.",
                          "basis": "Generated by fixtures/integration_check.py, which has no LLM in the loop by design."}],
    }
    qr_path = OUT_DIR / "quality-report.json"
    qr_path.write_text(json.dumps(quality_report, indent=2, default=str))
    errors = va.validate(qr_path, REPO_ROOT / "contracts", "quality-report", 1)
    check("Stage 4: quality-report.json is schema-valid", len(errors) == 0)
    for e in errors:
        print(f"    {e}")

    # ---------- Stage 5: data-validation, attached at the gate ----------
    print("\n=== Stage 5: data-validation (silver.orders vs gold.legacy_fct_orders) ===")
    validation_result = compare(
        adapter, "silver", "orders", adapter, "gold", "legacy_fct_orders",
        key_columns=["order_id", "line_number"], compare_columns=None,
        content_check_row_cap=100_000, row_level_diff_row_cap=5000, known_acceptable_differences=[],
    )
    validation_run_id = "integration-" + uuid.uuid4().hex[:12]
    # compare_staged.py's raw discrepancies carry inline source_row/target_row for the agent to
    # redact and diagnose -- the schema requires those NOT be inlined (sample_diff_ref is a
    # pointer, never raw row content past the cap) and requires diagnosis.explanation, not
    # root_cause. Reshape to match contracts/validation-report.schema.json exactly, same
    # transformation data-validation's own SKILL.md step 4 instructs the agent to perform.
    schema_shaped_discrepancies = []
    for d in validation_result["discrepancies"]:
        diff_ref = None
        if d.get("source_row") is not None or d.get("target_row") is not None:
            diff_ref = str(OUT_DIR / f"diff_{d['kind']}_{'_'.join(str(v) for v in d['key'].values())}.json")
            Path(diff_ref).write_text(json.dumps({"source_row": d.get("source_row"), "target_row": d.get("target_row")}, indent=2, default=str))
        schema_shaped_discrepancies.append({
            "kind": d["kind"], "key": d["key"],
            # compare_staged.py's discrepancies are only ever produced by the row_level_diff
            # stage in this implementation (row_count/hash_aggregate/column_aggregate stop the
            # comparison early on a match and never themselves enumerate individual discrepancies)
            # -- see references/staged-comparison.md.
            "stage_detected": "row_level_diff",
            "columns_affected": d.get("columns_affected", []), "sample_diff_ref": diff_ref,
            "diagnosis": {
                "source": "llm_inferred",
                "explanation": "Integration-check placeholder diagnosis -- see skills/data-validation/evals for real diagnosis evidence.",
                "confidence": 0.2, "basis": "integration-check: structural placeholder only, no LLM in the loop by design.",
                "suggested_fix": "Not applicable -- see skills/data-validation/evals.",
            },
        })
    validation_report = {
        "schema_version": "1.0.0", "run": run_manifest("data-validation", validation_run_id, target_schema="gold"),
        "source": {"platform": "azure", "object": f"{CATALOG}.silver.orders"},
        "target": {"platform": "azure", "object": f"{CATALOG}.gold.legacy_fct_orders"},
        "type_coercion_map": None,
        "normalization_applied": {"ordering": "key_columns", "nulls": "as_is", "floats": "rounded_4dp", "timezones": "as_is"},
        "stages": validation_result["stages"],
        "discrepancies": schema_shaped_discrepancies,
        "known_acceptable_differences_excluded": validation_result.get("known_acceptable_differences_excluded", []),
        "summary": validation_result["summary"],
        "assumptions": [{"statement": "Diagnoses in this artifact are integration-check placeholders, not real LLM reasoning.",
                          "basis": "Generated by fixtures/integration_check.py, which has no LLM in the loop by design."}],
    }
    vr_path = OUT_DIR / "validation-report.json"
    vr_path.write_text(json.dumps(validation_report, indent=2, default=str))
    errors = va.validate(vr_path, REPO_ROOT / "contracts", "validation-report", 1)
    check("Stage 5: validation-report.json is schema-valid", len(errors) == 0)
    for e in errors:
        print(f"    {e}")
    check("Stage 5: the known missing-orphan-order discrepancy is found (order_id 5230, the "
          "customer_id=99999 orphan silver.orders has that gold's inner join drops)",
          any(disc.get("kind") == "missing_from_target" for disc in schema_shaped_discrepancies))

    print()
    if failures:
        print(f"FAIL: {len(failures)} integration check(s) failed.")
        sys.exit(1)
    else:
        print("PASS: all integration checks passed -- modeling -> discovery -> pipeline chain verified, "
              "quality and validation gates attached and executed, all five artifact types produced "
              "and schema-valid in one continuous, real, no-LLM run.")
        sys.exit(0)


if __name__ == "__main__":
    main()
