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
    is_multi = spec.get("is_multi_source")
    lines = []
    for c in spec["columns"]:
        # A non-null "transformation" (contract's source.transformation, e.g.
        # "DATEDIFF(check_out, check_in)") is rendered via F.expr rather than a bare F.col alias --
        # json.dumps guarantees a correctly-escaped Python string literal, so the transformation
        # string (LLM/human-authored, untrusted-ish) can never break out of the F.expr(...) call no
        # matter what it contains. The SQL text itself still runs through Spark's own parser, same
        # trust boundary this toolkit already accepts for everything a human reviews before deploy.
        # A multi-source transformation is expected to already alias-qualify any column it
        # references itself (e.g. "CAST(header.OrderDate AS DATE)") -- this function has no way to
        # rewrite an arbitrary SQL expression's identifiers, so it passes it through verbatim
        # either way, same trust boundary as the single-source case.
        if c.get("transformation"):
            base = f'F.expr({json.dumps(c["transformation"])})'
        else:
            col_ref = f'{c["join_alias"]}.{c["source_column"]}' if is_multi and c.get("join_alias") else c["source_column"]
            base = f'F.col("{col_ref}")'
        if c.get("target_transform") == "hash":
            base = f'F.sha2({base}.cast("string"), 256)'
        lines.append(f'    {base}.alias("{c["target"]}")')
    return ",\n".join(lines)


def _join_condition_expr(cond: dict, right_alias: str) -> str:
    if cond.get("left_expression"):
        left = f'F.expr({json.dumps(cond["left_expression"])})'
    else:
        left = f'F.col("{cond["left_alias"]}.{cond["left_column"]}")'
    right = f'F.col("{right_alias}.{cond["right_column"]}")'
    return f'({left} == {right})'


def _source_read_expr(spec: dict, driving_streaming: bool) -> str:
    """The DataFrame expression a template reads from, before .select(...). Single-source: exactly
    today's bare spark.table(...)/spark.readStream.table(...) call. Multi-source: the driving
    object (streamed when driving_streaming, e.g. the merge_upsert declarative_pipeline staging
    view) .alias()'d and chained with one real .join(...) per declared source_joins entry.

    Every JOINED (non-driving) object is always read via spark.read.table(...) -- a static batch
    snapshot -- even when the driving side streams. This is the standard, documented
    stream-static join pattern for a many-to-one lookup (no watermark required, unlike a
    stream-stream join) and is exactly the shape a genuine lookup/denormalizing join needs; see
    references/declarative-pipelines.md. A join that needed the LOOKED-UP side to itself be
    incremental/CDC-aware would be a different, fan-out-risk shape this toolkit deliberately
    doesn't render -- see references/decision-rubric.md."""
    driving = spec["sources"][0]
    driving_ref = f'{driving["catalog"]}.{driving["schema"]}.{driving["table"]}'
    reader = "spark.readStream.table" if driving_streaming else "spark.table"
    if not spec.get("is_multi_source"):
        return f'{reader}("{driving_ref}")'

    lines = [f'{reader}("{driving_ref}").alias("{driving["alias"]}")']
    for j in spec["joins"]:
        join_ref = f'{j["catalog"]}.{j["schema"]}.{j["table"]}'
        conditions = " & ".join(_join_condition_expr(c, j["alias"]) for c in j["on"])
        lines.append(f'    .join(spark.read.table("{join_ref}").alias("{j["alias"]}"), {conditions}, "{j["join_type"]}")')
    return "\n".join(lines)


def _pii_transform_notes(spec: dict, modality: str) -> list[dict]:
    """Lakeflow Connect renders a raw connector-config stub (see
    templates/lakeflow_connect_config.yaml.tmpl) with no column-transform capability at all -- a
    modality-capability gap distinct from transform_spec's target_transform_gaps (a config gap:
    no rule was defined). A column tagged target_transform "hash" still can't actually be hashed
    for this modality, so that has to be surfaced too, separately."""
    if modality != "lakeflow_connect":
        return []
    return [
        {
            "column": c["target"],
            "reason": "not applied -- Lakeflow Connect performs raw ingestion with no "
                      "column-transform capability; the real target will contain untransformed PII.",
        }
        for c in spec["columns"] if c.get("target_transform") == "hash"
    ]


def _scd2_unsupported_notes(spec: dict, modality: str) -> list[dict]:
    """SCD Type 2 (stored_as_scd_type=2 + track_history_column_list) is a dlt.apply_changes
    feature -- declarative_pipeline modality only. pyspark_notebook's hand-rolled Delta MERGE has
    no built-in expire-and-insert-new-version semantics (that's genuinely complex_procedural logic,
    not something safe to template), and lakeflow_connect is raw ingestion with no transform
    capability at all (same reasoning as _pii_transform_notes). Surfaced as a gap, never silently
    ignored or guessed at."""
    if modality == "declarative_pipeline" or not spec.get("track_history_columns"):
        return []
    return [
        {
            "column": col,
            "reason": f"not applied -- SCD Type 2 history tracking is only auto-rendered for the "
                      f"declarative_pipeline modality (dlt.apply_changes); {modality} would need "
                      f"this hand-authored.",
        }
        for col in spec["track_history_columns"]
    ]


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
    # Only declarative_pipeline's merge_upsert staging view reads the driving object as a stream
    # (dlt.apply_changes' source); every other template/branch reads it as a plain batch table --
    # see _source_read_expr's docstring for why every JOINED object is always batch regardless.
    driving_streaming = modality == "declarative_pipeline" and bool(spec["merge_keys"])
    source_read_expr = _source_read_expr(spec, driving_streaming=driving_streaming)
    expectation_lines, tests_carried_forward = _expectation_lines(spec)
    pii_transform_notes = _pii_transform_notes(spec, modality)
    scd2_notes = _scd2_unsupported_notes(spec, modality)
    sequence_by_column = spec["merge_keys"][0] if spec["merge_keys"] else spec["columns"][0]["target"]

    stored_as_scd_type = spec.get("stored_as_scd_type", 1)
    track_history_columns = spec.get("track_history_columns") or []
    track_history_kwarg_line = (
        f'\n    track_history_column_list={json.dumps(track_history_columns)},'
        if stored_as_scd_type == 2 else ""
    )

    substitutions = {
        "target_table": spec["target_table"],
        "target_catalog": spec["target_catalog"],
        "target_schema": spec["target_schema"],
        "source_catalog": spec["source_catalog"],
        "source_schema": spec["source_schema"],
        "source_table": spec["source_table"],
        "source_read_expr": source_read_expr,
        "load_pattern": spec["load_pattern"],
        "merge_keys_pylist": merge_keys_pylist,
        "merge_keys_csv": merge_keys_csv,
        "select_lines": select_lines,
        "expectation_lines": expectation_lines,
        "sequence_by_column": sequence_by_column,
        "stored_as_scd_type": str(stored_as_scd_type),
        "track_history_kwarg_line": track_history_kwarg_line,
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
        "pii_transform_notes": pii_transform_notes,
        "scd2_notes": scd2_notes,
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
