#!/usr/bin/env python3
"""
derive_checks_from_contract.py -- converts a data-contract.json's tables[].tests[] into
data-quality check definitions. This is the explicit integration point between data-discovery
and data-quality the spec calls for: discovery already derives tests with a defensible
threshold_basis (contracts/confidence-rubric.md, contracts/data-contract.schema.json); quality
should run those same tests on a schedule rather than the two skills inventing separate,
possibly-inconsistent check logic.

Mapping (contract test type -> quality check type) -- names differ slightly because a contract
test describes a constraint to hold, while a quality check describes a scan to run:
    nullability -> null_rate     (max_null_rate straight from the contract test's params)
    uniqueness  -> uniqueness    (columns straight from the contract test's params)
    referential -> referential   (ref_object/ref_column straight from the contract test's params)
    range       -> value_range   (min/max straight from the contract test's params)
    freshness   -> freshness     (params passed through as-is)

severity is copied directly from the contract test -- quality does not re-decide how much a
check matters, discovery (or whoever authored the contract) already did.
"""
import argparse
import json
from pathlib import Path

TYPE_MAP = {
    "nullability": "null_rate",
    "uniqueness": "uniqueness",
    "referential": "referential",
    "range": "value_range",
    "freshness": "freshness",
}


def derive_checks(contract: dict, table_name: str) -> list[dict]:
    contract_id = contract["contract_id"]
    table = next((t for t in contract["tables"] if t["name"] == table_name), None)
    if table is None:
        raise ValueError(f"Table '{table_name}' not found in contract '{contract_id}'. "
                          f"Available: {[t['name'] for t in contract['tables']]}")

    checks = []
    for i, test in enumerate(table.get("tests", [])):
        quality_type = TYPE_MAP.get(test["type"])
        if quality_type is None:
            continue  # unknown contract test type -- skip rather than guess a quality check shape
        checks.append({
            "check_id": f"{table_name}.{test['column']}.{quality_type}.from_contract",
            "type": quality_type,
            "column": test["column"],
            "params": dict(test.get("params", {})),
            "severity": test["severity"],
            "derived_from_contract_test": {
                "contract_id": contract_id, "table": table_name, "test_index": i,
            },
        })
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("contract_json", type=Path)
    parser.add_argument("--table", required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    contract = json.loads(args.contract_json.read_text())
    checks = derive_checks(contract, args.table)
    output = json.dumps(checks, indent=2)
    if args.out:
        args.out.write_text(output)
    else:
        print(output)


if __name__ == "__main__":
    main()
