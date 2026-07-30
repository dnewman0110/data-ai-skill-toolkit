#!/usr/bin/env python3
"""
generate_pipeline_code.py -- renders transform_spec.json into the actual code files for the
CHOSEN modality, using the templates under skills/data-pipeline/templates/. This is the one
script in this skill that writes files a human might deploy -- see
references/toolkit-conventions.md #1: writing these files to output_dir is fine; nothing in this
script (or anywhere in this skill) deploys them, schedules them, or runs them.

Only uniqueness and nullability tests are auto-rendered into expectations (declarative_pipeline
modality) -- referential/range/freshness tests are recorded in tests_carried_forward with
generated_as "not carried forward" rather than guessed at, because expressing them correctly
(a referential expectation needs a subquery against the referenced table; a freshness expectation
needs a materialized "now" the template can't safely assume) is exactly the kind of judgment call
this toolkit's own rules say shouldn't be silently templated -- see DECISIONS.md.
"""
import argparse
import json
import string
import sys
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _select_lines(spec: dict) -> str:
    lines = [f'    F.col("{c["source_column"]}").alias("{c["target"]}")' for c in spec["columns"]]
    return ",\n".join(lines)


def _expectation_lines(spec: dict) -> tuple[str, list[dict]]:
    lines = []
    tests_carried_forward = []
    for t in spec.get("tests", []):
        if t["type"] == "uniqueness":
            cols = t.get("params", {}).get("columns", [t["column"]])
            not_null_expr = " AND ".join(f"{c} IS NOT NULL" for c in cols)
            key_desc = ",".join(cols)
            # Expectation key must be unique per test -- a table can have more than one candidate
            # key (a declared PK plus one or more natural-key-naming-heuristic hits, e.g.
            # silver.customers has three). A fixed "valid_grain" key for every uniqueness test
            # collided in a Python dict literal, silently keeping only the LAST one -- exactly the
            # kind of silent-drop this toolkit's own rules forbid. Keyed by the actual columns
            # involved instead, so every uniqueness test gets its own surviving entry.
            expectation_key = "valid_grain_" + "_".join(cols)
            lines.append(f'    "{expectation_key}": "{not_null_expr}"')
            tests_carried_forward.append({
                "source_test": f"uniqueness:{key_desc}",
                "generated_as": f"expect_or_drop('{expectation_key}', ...) -- checks key columns "
                                 "are non-null; true duplicate detection still relies on "
                                 "apply_changes' own key-based upsert, not a row-level expectation.",
            })
        elif t["type"] == "nullability":
            col = t["column"]
            max_null_rate = t.get("params", {}).get("max_null_rate")
            if max_null_rate == 0:
                lines.append(f'    "{col}_not_null": "{col} IS NOT NULL"')
                tests_carried_forward.append({
                    "source_test": f"nullability:{col}",
                    "generated_as": f"expect_or_fail('{col}_not_null', '{col} IS NOT NULL')",
                })
            else:
                tests_carried_forward.append({
                    "source_test": f"nullability:{col}",
                    "generated_as": f"not carried forward -- max_null_rate {max_null_rate} is a rate "
                                     f"threshold, not a per-row predicate; enforce via data-quality "
                                     f"instead of a Declarative Pipeline expectation.",
                })
        else:
            tests_carried_forward.append({
                "source_test": f"{t['type']}:{t['column']}",
                "generated_as": f"not carried forward -- {t['type']} tests are not auto-rendered "
                                 f"into pipeline expectations in this toolkit version; run "
                                 f"data-quality against the target after load instead.",
            })
    return (",\n".join(lines) if lines else ""), tests_carried_forward


def generate_pipeline_code(spec: dict, modality: str, output_dir: Path) -> dict:
    target_dir = output_dir / "generated" / spec["target_table"]
    target_dir.mkdir(parents=True, exist_ok=True)

    merge_keys_pylist = json.dumps(spec["merge_keys"])
    merge_keys_csv = ", ".join(spec["merge_keys"]) if spec["merge_keys"] else "(none -- full_refresh)"
    select_lines = _select_lines(spec)
    expectation_lines, tests_carried_forward = _expectation_lines(spec)
    sequence_by_column = spec["merge_keys"][0] if spec["merge_keys"] else spec["columns"][0]["target"]

    substitutions = {
        "target_table": spec["target_table"],
        "target_catalog": spec["target_catalog"],
        "target_schema": spec["target_schema"],
        "source_catalog": spec["source_catalog"],
        "source_schema": spec["source_schema"],
        "source_table": spec["source_table"],
        "load_pattern": spec["load_pattern"],
        "merge_keys_pylist": merge_keys_pylist,
        "merge_keys_csv": merge_keys_csv,
        "select_lines": select_lines,
        "expectation_lines": expectation_lines,
        "sequence_by_column": sequence_by_column,
    }

    generated_files = []

    if modality == "pyspark_notebook":
        tmpl = string.Template((TEMPLATES_DIR / "pyspark_notebook.py.tmpl").read_text())
        out_path = target_dir / "pyspark_notebook.py"
        out_path.write_text(tmpl.substitute(substitutions))
        generated_files.append({"path": str(out_path.relative_to(output_dir)), "modality": modality, "purpose": "pipeline_definition"})

    elif modality == "declarative_pipeline":
        # apply_changes requires at least one key column -- a target with no merge_keys
        # (load_pattern full_refresh, e.g. a generated calendar dimension or a small lookup table
        # with nothing to upsert on) gets the full-refresh template (a plain @dlt.table materialized
        # view) instead of the merge_upsert template, never apply_changes(keys=[]), which is invalid.
        if spec["merge_keys"]:
            tmpl = string.Template((TEMPLATES_DIR / "declarative_pipeline.py.tmpl").read_text())
        else:
            tmpl = string.Template((TEMPLATES_DIR / "declarative_pipeline_full_refresh.py.tmpl").read_text())
        out_path = target_dir / "declarative_pipeline.py"
        out_path.write_text(tmpl.substitute(substitutions))
        generated_files.append({"path": str(out_path.relative_to(output_dir)), "modality": modality, "purpose": "pipeline_definition"})

        exp_tmpl = string.Template((TEMPLATES_DIR / "expectations.py.tmpl").read_text())
        exp_path = target_dir / "expectations.py"
        exp_path.write_text(exp_tmpl.substitute(substitutions))
        generated_files.append({"path": str(exp_path.relative_to(output_dir)), "modality": modality, "purpose": "expectations"})

    elif modality == "lakeflow_connect":
        tmpl = string.Template((TEMPLATES_DIR / "lakeflow_connect_config.yaml.tmpl").read_text())
        out_path = target_dir / "connector_config.yaml"
        out_path.write_text(tmpl.substitute(substitutions))
        generated_files.append({"path": str(out_path.relative_to(output_dir)), "modality": modality, "purpose": "connector_config"})

    else:
        raise ValueError(f"Unknown modality '{modality}'.")

    spec_path = target_dir / "transform_spec.json"
    spec_path.write_text(json.dumps(spec, indent=2))

    return {
        "generated_files": generated_files,
        "transform_spec_ref": str(spec_path.relative_to(output_dir)),
        "tests_carried_forward": tests_carried_forward,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transform-spec-json", type=Path, required=True)
    parser.add_argument("--modality", required=True, choices=["pyspark_notebook", "declarative_pipeline", "lakeflow_connect"])
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    spec = json.loads(args.transform_spec_json.read_text())
    result = generate_pipeline_code(spec, args.modality, args.output_dir)
    print(json.dumps(result, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
