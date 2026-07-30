#!/usr/bin/env python3
"""
recommend_modality.py -- applies the toolkit's modality decision rubric deterministically, GIVEN
the classified rubric_factors. Classifying those factors (is the source a managed connector? is
the transform simple_declarative or complex_procedural? does this need streaming?) is judgment
the agent makes by reading the data-contract/model-spec -- see
references/decision-rubric.md. Applying the rubric to already-classified factors is a fixed rule,
so it lives here rather than in SKILL.md prose, per toolkit-conventions.md #5 (deterministic vs
LLM boundary): the classification step needs a confidence+basis in the final artifact; the rule
application below does not, because it is reproducible.

Rule, in priority order:
  1. If declarative_pipeline/lakeflow_connect are disabled in toolkit.yaml's environment block,
     they are never recommended regardless of other factors -- pyspark_notebook is the universal
     fallback and is always available.
  2. lakeflow_connect: only when the source is a managed-connector system AND no custom
     transformation is needed at ingestion (this always implies target_layer == bronze -- Lakeflow
     Connect lands data, it does not reshape it).
  3. pyspark_notebook: whenever the transform is complex_procedural or needs streaming with custom
     (non-CDC) logic -- Declarative Pipelines can't express arbitrary control flow or external calls.
  4. declarative_pipeline: the default for a simple_declarative transform once 1-3 don't apply --
     medallion bronze->silver->gold reshaping is exactly what it's built for, and idempotency comes
     largely for free (see references/idempotency-and-mock-data.md).
"""
import argparse
import json
import sys


def recommend_modality(rubric_factors: dict) -> dict:
    avail = rubric_factors["modality_availability"]
    source_is_connector = rubric_factors["source_is_managed_connector"]
    requires_streaming = rubric_factors["requires_streaming"]
    complexity = rubric_factors["transform_complexity"]
    target_layer = rubric_factors["target_layer"]

    if source_is_connector and target_layer == "bronze" and avail.get("lakeflow_connect"):
        return {
            "chosen": "lakeflow_connect",
            "rule_applied": "source_is_managed_connector and target_layer == bronze and lakeflow_connect available",
        }
    if complexity == "complex_procedural" or (requires_streaming and complexity == "complex_procedural"):
        return {
            "chosen": "pyspark_notebook",
            "rule_applied": "transform_complexity == complex_procedural (pyspark_notebook is the universal fallback, always available)",
        }
    if avail.get("declarative_pipeline"):
        return {
            "chosen": "declarative_pipeline",
            "rule_applied": "transform_complexity == simple_declarative and declarative_pipeline available (default for reshaping transforms)",
        }
    return {
        "chosen": "pyspark_notebook",
        "rule_applied": "transform_complexity == simple_declarative but declarative_pipeline is disabled in toolkit.yaml environment -- falling back to the universal modality",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rubric-factors-json", required=True, help="Path to a JSON file with the classified rubric_factors object.")
    args = parser.parse_args()
    with open(args.rubric_factors_json) as f:
        rubric_factors = json.load(f)
    result = recommend_modality(rubric_factors)
    print(json.dumps(result, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
