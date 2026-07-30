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
`toolkit.yaml`); the documented multi-source-join refusal; mock data respecting declared
nullability and uniqueness; the local idempotency proof (a real match on the example contract, and
a documented case showing the detector distinguishes "duplicate-but-consistent" from a genuine
mismatch); code generation for all three modalities with a real Python `compile()` syntax check on
every generated `.py` file; and a full transform-spec-to-validated-`pipeline-manifest.json`
end-to-end smoke test that also exercises `validate_artifact.py`. This is what
`.github/workflows/validate.yml` runs on every PR. 33 checks, all passing as of this build.

## 2. Scenario evals (`evals.json` / `eval_metadata.json`) -- require a subagent with the skill loaded

Five scenarios (one more than this toolkit's other skills, because the deploy-approval gate is a
high-consequence behavior worth its own dedicated check rather than folding it into the happy
path): the modality rubric choosing correctly on a normal reshaping target (eval 1, using the same
`fct_orders` contract every other skill's evals reference), choosing `lakeflow_connect` for a
genuinely managed-connector bronze landing (eval 2, using a purpose-built fixture contract under
`evals/fixtures/` since the toolkit's own fixture lakehouse has no external-system source), routing
a multi-source join to `pyspark_notebook` rather than forcing a broken single-source spec (eval 3,
same fixture pattern), declining to re-enter code-generation mode for what's actually a
post-load correctness question (eval 4), and -- the one most worth getting right in this skill --
refusing to advance past `validated` on a vague "go ahead and deploy it" that doesn't name a
specific target (eval 5).

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

To re-run a scenario eval yourself: spawn an agent with access to this repo, point it at
`skills/data-pipeline/SKILL.md`, and give it the `prompt` field from the matching entry in
`evals.json`. Grade the output against that eval's `assertions` in `eval_metadata.json`.
