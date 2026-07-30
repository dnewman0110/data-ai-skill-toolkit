---
name: data-pipeline
description: >-
  Generates pipeline code (PySpark notebook, Lakeflow Declarative Pipeline, or Lakeflow Connect
  config -- chosen via an explicit decision rubric, never guessed) from a data-contract.json (and
  optionally a model-spec.json for context), plus synthetic mock data and local idempotency
  evidence for the generated merge/upsert logic. Use this once a data contract exists and a human
  is ready to move from "here's what the target should look like" to "here's the code that would
  build it." Produces pipeline-manifest.json plus the code files themselves under
  output_dir/generated/. This is the only skill in the toolkit that writes code to disk, and even
  it never deploys, schedules, or executes anything against a real target -- that always requires
  a separate, explicit human approval naming the specific target. Do NOT use this to design a
  dimensional model (that's data-modeling) or to decide what a contract's tables/columns/tests
  should be (that's data-discovery) -- this skill only ever renders what a contract already
  specifies. Do NOT use this to check whether a table this skill's own code populated is
  correct -- that's data-quality (single object) or data-validation (source vs. target), run
  after code has actually been deployed and executed by someone else.
version: 1.0.0
---

# data-pipeline

This skill ships as part of the `data-ai-skill-toolkit` plugin. Commands below use
`${CLAUDE_PLUGIN_ROOT}` for the toolkit-root shared paths (`scripts/`, `contracts/`) and
`${CLAUDE_SKILL_DIR}` for files bundled with this skill specifically -- both resolve correctly
regardless of the working directory or which repo this plugin is installed into. See the
top-level `README.md` "Installing as a plugin" for how this toolkit is packaged.

## Read/write boundary

**This skill generates code and writes files to `output_dir`. It never touches client systems.**
Concretely: it does not run DDL or DML against any real catalog, does not create or schedule a
Databricks Job, does not invoke `databricks bundle deploy` or any deployment API, and does not
execute a byte of the code it generates -- against mock data or anything else. The one thing this
skill DOES execute is its own local idempotency proof (`validate_pipeline_locally.py`), and that
runs entirely against synthetic mock data in an in-memory scratch SQLite database, never against a
client system.

Every one of those write/deploy/execute actions requires an explicit, in-conversation human
approval that **names the specific target object or job** -- a generic "looks good, ship it" does
not count, and this skill never infers approval from silence. Even with approval, this skill only
*records* it (`deployment.approved`, `deployment.target_named`, readiness_level
`approved_for_deployment`) -- it does not perform the deployment itself. Per
`references/toolkit-conventions.md` #7 gate 3: "a human (or a separate, explicitly authorized
process) takes it from there." `readiness_level: deployed` is set by whatever actually deploys,
not by this skill.

## What this produces

Per run: one `pipeline-manifest.json` (schema: `contracts/pipeline-manifest.schema.json`) covering
one or more target tables, plus, under `output_dir/generated/<table>/`: the modality-specific code
file(s), a `transform_spec.json` (the portable logic those files were rendered from), and an
`idempotency_evidence.json`. Mock data lives under `output_dir/mock_data/`.

`data-pipeline` has no dedicated schema in the original five-schema Phase 0 set -- it produces
code, not a lineage/comparison artifact like the other four skills. `pipeline-manifest.schema.json`
was added in Phase 2 specifically to give this skill's run the same auditable, versioned,
diffable record the other four get. See `DECISIONS.md`.

## Workflow

1. **Load the data-contract.** `source_refs.data_contract_ref` is required -- this skill never
   generates code with no upstream contract. Validate it first
   (`python "${CLAUDE_PLUGIN_ROOT}/scripts/validate_artifact.py" <contract> --schema-type data-contract --supported-major 1`)
   and halt if it fails or if its schema major version is unsupported. If a `model-spec.json` is
   also available, read it for business context (measure additivity, SCD rationale) but the
   contract remains the source of truth for what gets built -- **the human review gate between
   contract and pipeline generation (`references/toolkit-conventions.md` #7 gate 1) has already
   happened by the time this skill runs; this skill does not re-litigate mapping confidence.**

2. **Classify the modality rubric factors, then apply the rubric.** For each target table, read
   the contract (and `toolkit.yaml`'s `environment.declarative_pipelines_available` /
   `lakeflow_connect_available` flags) and classify:
   - `source_is_managed_connector`: is the source object from a system Lakeflow Connect has a
     managed connector for, with no transformation needed at ingestion?
   - `requires_streaming`: does the target need continuous/low-latency refresh?
   - `transform_complexity`: `simple_declarative` (expressible as select/join/aggregate/merge) or
     `complex_procedural` (needs control flow, external calls, or logic a declarative framework
     can't express)?
   - `target_layer`: `bronze` / `silver` / `gold`.

   This classification is judgment (per `references/toolkit-conventions.md` #5) -- see
   `references/decision-rubric.md` for worked examples of each factor. Once classified, call:
   ```
   python "${CLAUDE_SKILL_DIR}/scripts/recommend_modality.py" --rubric-factors-json <factors.json>
   ```
   which applies the rubric deterministically. Record the chosen modality, the rubric_factors, and
   a `confidence`+`basis` for the classification step (not the rule application) in the final
   manifest's `modality_decision`.

3. **Run the pipeline build for each target table:**
   ```
   python "${CLAUDE_SKILL_DIR}/scripts/build_pipeline_manifest.py" \
     --contract-json <contract> --table <table_name> --modality <chosen> \
     --output-dir <output_dir> --out <output_dir>/pipeline_findings_<table>.json
   ```
   This derives the portable transform spec, synthesizes mock data, proves local idempotency
   (or determines it's `not_applicable` for `lakeflow_connect`), and renders the modality's code
   files. It never chooses the modality itself -- you pass it in from step 2.

4. **Read `pipeline_findings_<table>.json`.** Check `idempotency_check.result`:
   - `match` or `not_applicable`: proceed, this target is eligible for `readiness_level: validated`.
   - `mismatch`: **halt on this target.** Do not advance its readiness past `draft`. Report the
     evidence file and do not guess at a fix -- the generated merge logic disagreeing with itself
     on a second run is exactly the kind of confidently-wrong artifact `references/toolkit-conventions.md`
     #6 says to halt on, not paper over.

   Also check `low_confidence_mappings` -- any target column whose contract mapping was
   `llm_inferred` with confidence below 0.5 gets an entry in the final manifest's `assumptions[]`
   noting the generated code trusts that mapping as-is (see the example artifact).

5. **Assemble `pipeline-manifest.json`** matching `contracts/pipeline-manifest.schema.json`:
   `targets[]` from each `pipeline_findings_<table>.json`'s `target`, `mock_data` and
   `idempotency_check` per target (if multiple targets disagree on idempotency result, report the
   worst case at the run level and detail per-target in `assumptions[]`), `readiness_level`
   (`validated` only if every target's idempotency check passed or was not_applicable AND every
   file compiled/parsed cleanly; `draft` otherwise), `deployment: null` (see below), then validate:
   ```
   python "${CLAUDE_PLUGIN_ROOT}/scripts/validate_artifact.py" <output>/pipeline-manifest.json --schema-type pipeline-manifest --supported-major 1
   ```
   Do not declare the run successful until this passes.

6. **If a previous run exists for this pipeline_id**, diff against it:
   ```
   python "${CLAUDE_PLUGIN_ROOT}/scripts/diff_artifact.py" <previous>/pipeline-manifest.json <new>/pipeline-manifest.json --schema-type pipeline-manifest --out <new>/pipeline-manifest.diff.json
   ```
   A modality change is the loudest signal here -- it means every generated file for that target
   was replaced, not incrementally edited. Never overwrite the previous artifact or its generated
   files; new runs get a new `run_id` directory.

7. **Report to the human, and stop.** Summarize: which modality was chosen and why, what files
   were generated and where, the idempotency evidence, and any `low_confidence_mappings` or
   halted targets. **Explicitly ask** whether and where to move toward deployment -- do not offer
   to deploy proactively. Only if the human's response, in this conversation, names a specific
   target job/object and says to proceed: update `deployment` (`approved: true`, `approved_by`,
   `target_named` echoing exactly what they named, `approval_note`, `approved_at`) and set
   `readiness_level: approved_for_deployment` in a new manifest version. Still do not run, deploy,
   or schedule anything -- there is no script in this skill that does that, by design.

## When NOT to use this skill

- Deciding what a target's tables, columns, grain, or tests should be -- that's
  **`data-discovery`** (or **`data-modeling`** for the dimensional design that feeds discovery in
  resolution mode). This skill renders what a `data-contract.json` already specifies; it does not
  invent or revise mappings.
- Checking whether data this skill's generated code produced is actually correct -- that's
  **`data-quality`** (single object) or **`data-validation`** (source vs. target), and only after
  a human has taken the generated code from here to an actual deployment and run.
- Multi-source joins. `build_transform_spec.py` only supports single-source targets in this
  version -- a target whose columns come from more than one source object needs hand-authored
  join logic (`transform_complexity: complex_procedural`, `pyspark_notebook` modality, written by
  a human starting from this skill's column mapping as a reference, not generated end-to-end).

## Reference material

- `references/decision-rubric.md` -- worked examples for classifying each rubric factor, and why
  the priority order in `recommend_modality.py` is ordered the way it is.
- `references/pyspark-notebook.md` -- when this is the right (or forced) choice, idempotency and
  restart semantics, what the template does and doesn't handle.
- `references/declarative-pipelines.md` -- `apply_changes`/CDC semantics, `sequence_by`, why
  idempotency is largely structural here.
- `references/lakeflow-connect.md` -- why this modality generates a config stub rather than code,
  and what "idempotency not_applicable" means for a managed connector.
- `references/other-modalities.md` -- streaming_cdc load pattern and multi-source joins: documented
  gaps in this version, not silently unsupported.
- `references/idempotency-and-mock-data.md` -- exactly what the local mock-data proof does and does
  not establish, and how mock data is derived from a contract's declared types/nullability/tests.
- `references/toolkit-conventions.md` -- cross-cutting rules shared by all five skills.
