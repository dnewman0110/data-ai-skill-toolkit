#!/usr/bin/env python3
"""
build_findings.py -- the one command SKILL.md tells the agent to run. Orchestrates the fully
deterministic half of discovery: cost-gate, profile every target object, propose tests, collect
findings, redact samples. Produces discovery_findings.json, which the agent then reads to do the
one part that isn't deterministic -- for greenfield mode, proposing which source columns map to
which target concept and why (mapping_type=llm_inferred, with confidence + basis per
confidence-rubric.md); for resolution mode, resolving a model-spec's required fields against
these same findings and reporting anything it can't satisfy. The agent assembles the final
data-contract.json from this file plus its own reasoning, then validates it with
scripts/validate_artifact.py before declaring success -- this script does not, itself, produce a
schema-valid data-contract.json, on purpose (see references/toolkit-conventions.md #5, the
deterministic-vs-LLM boundary, and SKILL.md step 4).

Exit behavior: if the pre-flight cost estimate would exceed configured thresholds, this script
prints the decision and exits nonzero WITHOUT profiling anything. SKILL.md instructs the agent to
stop and surface that to the user rather than retrying with --force.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from lakehouse_adapter import build_adapter  # noqa: E402
from estimate_scan_cost import estimate_and_gate  # noqa: E402
from redact import redact_rows  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from profile_object import profile_table  # noqa: E402
from propose_tests import propose_tests  # noqa: E402


def build_findings(adapter, targets: list[tuple[str, str]], thresholds: dict,
                    sample_size: int, max_sample_records: int, sensitive_columns: list[dict],
                    candidate_fks_by_table: dict | None = None, force: bool = False) -> dict:
    cost_decision = estimate_and_gate(adapter, targets, thresholds)
    if not cost_decision["proceed"] and not force:
        return {
            "halted": True,
            "reason": "cost_threshold_exceeded",
            "cost_decision": cost_decision,
            "tables": [],
        }

    candidate_fks_by_table = candidate_fks_by_table or {}
    tables = []
    for schema, table in targets:
        profile = profile_table(adapter, schema, table, sample_size,
                                 candidate_fks=candidate_fks_by_table.get(f"{schema}.{table}"))
        proposal = propose_tests(profile)
        sample_cols = [c["name"] for c in profile["columns"]]
        raw_samples = adapter.sample_rows(schema, table, sample_cols, max_sample_records)
        redacted_samples = redact_rows(raw_samples, sensitive_columns, max_sample_records)
        tables.append({
            "object": profile["object"],
            "row_count": profile["row_count"],
            "table_comment": profile["table_comment"],
            "columns": profile["columns"],
            "candidate_keys": profile["candidate_keys"],
            "fk_checks": profile["fk_checks"],
            "proposed_tests": proposal["tests"],
            "findings": proposal["findings"],
            "sample_records": redacted_samples,
        })

    return {"halted": False, "cost_decision": cost_decision, "tables": tables}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["sqlite_fixture", "databricks_connect", "sqlserver"], default="sqlite_fixture",
                         help="From toolkit.yaml's environment.backend (sqlite_fixture/databricks_connect) "
                              "or external_sources.sqlserver.enabled (sqlserver, for pre-ingestion "
                              "profiling -- see references/sqlserver-profiling.md). sqlite_fixture "
                              "(evals/local dev, needs --lakehouse-dir), databricks_connect (production, "
                              "via an already-authenticated Databricks Connect session), or sqlserver "
                              "(needs --sqlserver-host/--sqlserver-database, plus auth per --sqlserver-auth-mode).")
    parser.add_argument("--lakehouse-dir", default=None, help="Required when --backend sqlite_fixture.")
    parser.add_argument("--catalog", default="acme_retail_dev", help="Ignored when --backend sqlserver -- use --sqlserver-database instead.")
    parser.add_argument("--sqlserver-host", default=None, help="Required when --backend sqlserver. From toolkit.yaml's external_sources.sqlserver.host.")
    parser.add_argument("--sqlserver-database", default=None, help="Required when --backend sqlserver. From toolkit.yaml's external_sources.sqlserver -- not a secret.")
    parser.add_argument("--sqlserver-driver", default="ODBC Driver 18 for SQL Server")
    parser.add_argument("--sqlserver-port", type=int, default=1433)
    parser.add_argument("--sqlserver-auth-mode", choices=["azure_ad_default", "sql_auth_env", "windows_integrated"], default="azure_ad_default")
    parser.add_argument("--sqlserver-username-env-var", default=None,
                         help="sql_auth_env only. NAME of an environment variable holding the username -- never the value itself.")
    parser.add_argument("--sqlserver-password-env-var", default=None,
                         help="sql_auth_env only. NAME of an environment variable holding the password -- never the value itself.")
    parser.add_argument("--target", action="append", required=True, help="schema.table, repeatable")
    parser.add_argument("--candidate-fk", action="append", default=[],
                         help="schema.table:column:ref_schema.ref_table.ref_column, repeatable")
    parser.add_argument("--sample-size", type=int, default=None)
    parser.add_argument("--max-sample-records", type=int, default=20)
    parser.add_argument("--max-rows-scanned", type=int, default=None)
    parser.add_argument("--max-bytes-scanned", type=int, default=None)
    parser.add_argument("--sensitive-columns-json", type=Path, default=None,
                         help="JSON file: toolkit.yaml's sample_data.sensitive_columns list")
    parser.add_argument("--force", action="store_true", help="Proceed even if the cost gate says no. Use only after explicit human confirmation.")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    if args.backend == "sqlite_fixture" and not args.lakehouse_dir:
        parser.error("--lakehouse-dir is required when --backend sqlite_fixture")
    if args.backend == "sqlserver" and not (args.sqlserver_host and args.sqlserver_database):
        parser.error("--sqlserver-host and --sqlserver-database are required when --backend sqlserver")

    targets = [tuple(t.split(".", 1)) for t in args.target]

    candidate_fks_by_table = {}
    for spec in args.candidate_fk:
        table_part, col, ref = spec.split(":", 2)
        ref_schema, ref_table, ref_column = ref.split(".")
        candidate_fks_by_table.setdefault(table_part, []).append(
            {"column": col, "ref_schema": ref_schema, "ref_table": ref_table, "ref_column": ref_column}
        )

    sensitive_columns = []
    if args.sensitive_columns_json:
        sensitive_columns = json.loads(args.sensitive_columns_json.read_text())

    adapter = build_adapter(
        args.backend, lakehouse_dir=args.lakehouse_dir, catalog=args.catalog,
        sqlserver_host=args.sqlserver_host, sqlserver_database=args.sqlserver_database,
        sqlserver_driver=args.sqlserver_driver, sqlserver_port=args.sqlserver_port,
        sqlserver_auth_mode=args.sqlserver_auth_mode,
        sqlserver_username_env_var=args.sqlserver_username_env_var,
        sqlserver_password_env_var=args.sqlserver_password_env_var,
    )
    result = build_findings(
        adapter, targets,
        thresholds={"max_rows_scanned": args.max_rows_scanned, "max_bytes_scanned": args.max_bytes_scanned},
        sample_size=args.sample_size, max_sample_records=args.max_sample_records,
        sensitive_columns=sensitive_columns, candidate_fks_by_table=candidate_fks_by_table,
        force=args.force,
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
