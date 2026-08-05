# data-pipeline evals

Two layers, matching how much of the skill's behavior is deterministic vs. requires reasoning --
same structure as every other skill's evals, but this skill's deterministic layer covers more
ground than its siblings' because it does real, checkable work (rendering code, proving local
idempotency) rather than only measurement.

## 1. Deterministic assertions (`run_assertions.py`) -- no subagent, no LLM, CI-runnable

```
python fixtures/generate_fixtures.py
python skills/data-pipeline/evals/run_assertions.py
```

Covers: the write boundary (this skill is the one skill allowed to write, so the check here is
narrower than its read-only siblings' -- confirms `build_transform_spec.py`, `derive_mock_data.py`,
`recommend_modality.py`, and `generate_pipeline_code.py` contain no write-shaped SQL at all, and
that `validate_pipeline_locally.py`'s only database connection is a literal in-memory SQLite
scratch, never a file path or real target); malformed/unsupported-major artifact rejection; the
modality rubric's full priority order (managed-connector+bronze -> lakeflow_connect,
complex_procedural -> pyspark_notebook regardless of availability, simple_declarative defaults to
declarative_pipeline, and falls back to pyspark_notebook when declarative_pipeline is disabled in
`toolkit.yaml`); mock data respecting declared nullability and uniqueness; the local idempotency
proof (a real match on the example contract, and a documented case showing the detector
distinguishes "duplicate-but-consistent" from a genuine mismatch); code generation for all three
modalities with a real Python `compile()` syntax check on every generated `.py` file; a full
transform-spec-to-validated-`pipeline-manifest.json` end-to-end smoke test that also exercises
`validate_artifact.py`; derived-column transformation rendering (`F.expr(...)`), the type-mismatch
gate (flags and caps `readiness_level`, never crashes), SCD Type 2 rendering and its scoping to
`declarative_pipeline` only, and the all-merge-key bridge-table fix (DECISIONS.md decision 56); and
-- added for the multi-source join support in DECISIONS.md's join-support decision -- a declared
`table.source_joins` actually rendering a real multi-table join for both worked shapes (a
denormalizing self-join dimension, a header-rollup-plus-expression-based-dimension-lookup fact,
both compiling and both feeding a full `build_pipeline_findings` run through to a schema-valid
manifest with `idempotency_check.result: not_applicable` and `mock_data.generated: false`, never a
fabricated `match`), plus every multi-source refusal path (no `source_joins` declared, a
`source_joins` declaration inconsistent with the columns it's supposed to explain, a duplicate
alias, an alias referencing something not yet introduced, `source_joins` declared when only one
source object is actually in play). This is what `.github/workflows/validate.yml` runs on every
PR. 75 checks, all passing as of this build.

## 2. Scenario evals (`evals.json` / `eval_metadata.json`) -- require a subagent with the skill loaded

Eleven scenarios: the modality rubric choosing correctly on a normal reshaping target (eval 1,
using the same `fct_orders` contract every other skill's evals reference), choosing
`lakeflow_connect` for a genuinely managed-connector bronze landing (eval 2, using a purpose-built
fixture contract under `evals/fixtures/` since the toolkit's own fixture lakehouse has no
external-system source), routing a multi-source join with NO declared `source_joins` to
`pyspark_notebook` rather than forcing a broken single-source spec or guessing a join condition
(eval 3, same fixture pattern -- renamed from "routes-to-pyspark" once multi-source joins with a
DECLARED join became supported, see evals 10-11), declining to re-enter code-generation mode for
what's actually a post-load correctness question (eval 4), refusing to advance past `validated` on
a vague "go ahead and deploy it" that doesn't name a specific target (eval 5), real target-data
hashing actually applying when `toolkit.yaml` enables it (eval 6, using
`evals/fixtures/pii-hashing-contract.json`) and the gap being surfaced loudly in
`pii_transform_gaps` rather than silently guessed or silently dropped when it's disabled (eval 7,
same fixture), a derived transformation rendering correctly alongside a type mismatch capping
`readiness_level` at `draft` (eval 8, using `evals/fixtures/derived-transformation-contract.json`),
SCD Type 2 rendering for `declarative_pipeline` while `pyspark_notebook` surfaces it as an
unsupported gap instead of silently ignoring it (eval 9, using
`evals/fixtures/scd2-dimension-contract.json`), and -- added for the multi-source join support --
a denormalizing dimension join with a self-join needing two distinct aliases (eval 10, using
`evals/fixtures/denormalizing-dimension-join-contract.json`) and a fact whose attributes roll up
from a header table plus an expression-keyed dimension lookup (eval 11, using
`evals/fixtures/header-rollup-dimension-lookup-contract.json`).

**Phase 2 sign-off evidence**: eval 1 (the fullest scenario -- modality classification, code
generation, idempotency evidence, and the deployment-gate language all in one run) was run as a
full subagent invocation and graded against every assertion in `eval_metadata.json`, independently
re-verified with `scripts/validate_artifact.py` and by inspecting the generated files and manifest
fields directly rather than trusting the subagent's own claim of success. Evals 2 and 3's
*mechanisms* (the rubric choosing `lakeflow_connect`/`pyspark_notebook` correctly, the
multi-source refusal firing) are already covered by the deterministic suite in `run_assertions.py`
against the same fixture contracts; evals 4 and 5 rely on the skill description, the
`## When NOT to use this skill` section, and the read/write boundary language in `SKILL.md`,
consistent with how the prior three skills' Phase 1/2 sign-offs handled their own equivalent
cases. Re-run 2, 3, 4, and 5 as full subagent scenarios before this touches a real engagement --
eval 5 in particular is worth re-running any time `SKILL.md`'s deployment-gate language changes.
Evals 6-9's *mechanisms* (hash rendering, `target_transform_gaps`/`type_mismatch_gaps`/
`scd2_unsupported_notes` population, `F.expr` rendering, SCD Type 2 template substitution) were all
verified directly against `build_transform_spec`/`generate_pipeline_code`/`build_pipeline_manifest`
rather than via a subagent run when each was added -- run 6-9 as full subagent scenarios at least
once before this touches a real engagement, same as 2-5. Evals 10-11's *mechanisms* (a declared
`source_joins` rendering a real multi-table join, the self-join alias disambiguation, the
expression-keyed join condition, the `not_applicable` idempotency result) are likewise already
verified directly in `run_assertions.py` end-to-end (`build_transform_spec` ->
`generate_pipeline_code` -> `build_pipeline_findings` -> a real `compile()` check on the generated
code) against both fixture contracts -- run 10-11 as full subagent scenarios at least once before
this touches a real engagement, same discipline as every other not-yet-subagent-run eval here.

To re-run a scenario eval yourself: spawn an agent with access to this repo, point it at
`skills/data-pipeline/SKILL.md`, and give it the `prompt` field from the matching entry in
`evals.json`. Grade the output against that eval's `assertions` in `eval_metadata.json`.
