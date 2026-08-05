---
name: data-deploy
description: >-
  Turns a data-pipeline-generated pipeline-manifest.json whose modality_decision.chosen is
  lakeflow_connect and readiness_level is approved_for_deployment into real Databricks Asset
  Bundle resources (a Unity Catalog connection definition plus an ingestion-pipeline/destination
  resource) and a resolved Lakeflow Connect connector type -- generic across source systems (SQL
  Server, Salesforce, ServiceNow, Workday, SharePoint, extensible via
  references/connector-type-mapping.md), never specific to one engagement's source system or
  table list. Use this once a pipeline manifest exists, is approved for deployment, and names a
  specific target -- this skill refuses to touch any target not named in that approval. Produces
  deployment-manifest.json plus the bundle resource files themselves under
  output_dir/generated/. Like data-pipeline, this skill never deploys, schedules, executes, or
  creates anything against a real target -- actually running `databricks bundle deploy` or
  creating a live connector requires a SEPARATE, explicit human approval this skill only records,
  never performs. Do NOT use this to generate pipeline code, choose a modality, or decide what a
  contract's tables/columns should be (that's data-pipeline/data-discovery/data-modeling) -- this
  skill only ever turns an already-approved lakeflow_connect pipeline manifest into deployable
  bundle resources. Do NOT use this for a pipeline-manifest whose modality is pyspark_notebook or
  declarative_pipeline -- those deploy through whatever mechanism the target project's own CI/CD
  already uses for Databricks Jobs/DLT, not through this skill.
version: 1.0.0
---

# data-deploy

This skill ships as part of the `data-ai-skill-toolkit` plugin. Commands below use
`${CLAUDE_PLUGIN_ROOT}` for the toolkit-root shared paths (`scripts/`, `contracts/`) and
`${CLAUDE_SKILL_DIR}` for files bundled with this skill specifically -- both resolve correctly
regardless of the working directory or which repo this plugin is installed into. See the
top-level `README.md` "Installing as a plugin" for how this toolkit is packaged.

## Read/write boundary

**This skill generates Asset Bundle resource files and writes them to `output_dir`. It never
touches client systems.** Concretely: it does not run `databricks bundle deploy` or `databricks
bundle validate` against any real workspace, does not create a Unity Catalog connection, does not
create or start a Lakeflow Connect ingestion pipeline, does not call any Databricks API, and does
not edit a client project's own `databricks.yml` (that file lives outside `output_dir`; a human
wires the generated resource file into it).

This skill also does not run at all unless the source `pipeline-manifest.json` already carries an
explicit, in-conversation human approval naming a specific target
(`deployment.approved: true`, `deployment.target_named`) -- and even then, generating bundle
resources is a SEPARATE thing from actually deploying them. A further, separate, explicit human
approval is required before anything in the generated resources is applied to a real workspace,
and this skill only *records* that second approval (`deployment.approved`,
`deployment.target_named`, `readiness_level: approved_for_deployment` in `deployment-manifest.json`)
-- it never performs the deploy itself. See `references/approval-gate.md` for the full reasoning
on why two separate approvals, and why this skill matches (rather than departs from) every other
skill's "generate, never deploy" boundary.

## What this produces

Per run: one `deployment-manifest.json` (schema: `contracts/deployment-manifest.schema.json`)
covering exactly one target (the one named in the source pipeline-manifest's approval; every other
target in that manifest is recorded `skipped: true` with a reason, never silently dropped), plus,
under `output_dir/generated/<table_name>/`: `uc_connection.yml` and `ingestion_pipeline.yml` --
see `references/asset-bundle-resources.md` for exactly what each contains.

## Input preconditions

`scripts/check_target_approval.py` enforces all of these before anything else runs. Any one
failing halts the run with a specific reason -- this skill never proceeds on a partial match:

1. `pipeline-manifest.modality_decision.chosen == "lakeflow_connect"` -- this skill only knows how
   to turn Lakeflow Connect manifests into bundle resources.
2. `pipeline-manifest.readiness_level == "approved_for_deployment"`.
3. `pipeline-manifest.deployment.approved == true`, with a non-empty `deployment.target_named`.
4. `deployment.target_named` matches exactly one `table_name` in the pipeline-manifest's
   `targets[]` -- an approval naming a target that doesn't exist in the manifest halts rather than
   guessing which target was meant.

## Workflow

1. **Load and validate the source pipeline-manifest.** Validate it first
   (`python "${CLAUDE_PLUGIN_ROOT}/scripts/validate_artifact.py" <pipeline-manifest.json> --schema-type pipeline-manifest --supported-major 1`)
   and halt if it fails or if its schema major version is unsupported.

2. **Determine `source_system` for the approved target.** This is judgment, not something parsed
   from the data-contract (see `references/connector-type-mapping.md` for exactly why
   `source.object`'s naming convention isn't reliable enough to string-match) -- read it off
   engagement context (a human already told you this is the Salesforce pipeline, or the SQL Server
   one), or ask if it's genuinely ambiguous. Also determine `connection_name`: the Unity Catalog
   connection name this target's ingestion should use, either an existing connection the human
   names or a new one to document (engagement-specific naming, never invented silently).

3. **Run the deployment build:**
   ```
   python "${CLAUDE_SKILL_DIR}/scripts/build_deployment_manifest.py" \
     --pipeline-manifest-json <pipeline-manifest.json> \
     --pipeline-output-dir <the output_dir the pipeline-manifest's transform_spec_ref paths are relative to> \
     --source-system <name from step 2> \
     --connection-name <name from step 2> \
     --output-dir <output_dir> --out <output_dir>/deployment_findings.json
   ```
   This runs the approval gate (halts per "Input preconditions" above if not satisfied), resolves
   the connector type, and renders both bundle resource files for the one approved target. If
   `source_system` isn't recognized, the run does not halt outright -- it reports
   `unsupported_source_systems` with a reason, so a manifest with one bad target name doesn't block
   diagnosing everything else about the run. Never guess a fallback connector type for it.

4. **Read `deployment_findings.json`.** If `halted: true`, report the reason and stop -- do not
   attempt to work around a failed precondition. If `unsupported_source_systems` is non-empty,
   report it and stop for that target; do not fall back to a generic or best-guess connector
   configuration.

5. **Assemble `deployment-manifest.json`** matching `contracts/deployment-manifest.schema.json`:
   `source_pipeline_manifest_ref` (the source manifest's `pipeline_id`, `schema_version`, path),
   `approval_gate` (echoing `deployment_findings.json`'s `approval_gate` -- `source_target_named`,
   `source_approved_by`, `source_approved_at`), `targets[]` (the processed target plus every
   skipped target from `deployment_findings.json`), `unsupported_source_systems`,
   `readiness_level` (`validated` only if the processed target's `generated_files` all exist on
   disk and parse as valid YAML; `draft` otherwise), `deployment: null` (see step 6), then
   validate:
   ```
   python "${CLAUDE_PLUGIN_ROOT}/scripts/validate_artifact.py" <output>/deployment-manifest.json --schema-type deployment-manifest --supported-major 1
   ```
   Do not declare the run successful until this passes.

6. **If a previous run exists for this `deployment_id`**, diff against it:
   ```
   python "${CLAUDE_PLUGIN_ROOT}/scripts/diff_artifact.py" <previous>/deployment-manifest.json <new>/deployment-manifest.json --schema-type deployment-manifest --out <new>/deployment-manifest.diff.json
   ```
   Never overwrite the previous artifact or its generated files; new runs get a new `run_id`
   directory.

7. **Report to the human, and stop.** Summarize: the resolved connector type, what files were
   generated and where, which targets (if any) were skipped and why, and any unsupported source
   systems. Point at `references/asset-bundle-resources.md`'s note to run `databricks bundle
   validate` before deploying, and that a UC connection generally needs to be created once, out of
   band, before the ingestion pipeline resource can actually deploy. **Explicitly ask** whether and
   where to move toward actually running `databricks bundle deploy` -- do not offer to run it
   proactively. Only if the human's response, in this conversation, names a specific target and
   says to proceed: update `deployment` (`approved: true`, `approved_by`, `target_named` echoing
   exactly what they named, `approval_note`, `approved_at`) and set
   `readiness_level: approved_for_deployment` in a new manifest version. Still do not run, deploy,
   or create anything -- there is no script in this skill that does that, by design. See
   `references/approval-gate.md`.

## When NOT to use this skill

- A pipeline-manifest whose `modality_decision.chosen` is `pyspark_notebook` or
  `declarative_pipeline` -- those deploy through Databricks Jobs / DLT pipeline creation, a
  different mechanism this skill doesn't model. `check_target_approval.py` halts on this rather
  than attempting it.
- Deciding what a target's tables, columns, grain, or tests should be, or which modality a
  pipeline should use -- that's **`data-discovery`**, **`data-modeling`**, and **`data-pipeline`**.
  This skill only renders bundle resources for a modality decision and target list
  `data-pipeline` already made and a human already approved.
- Actually running `databricks bundle deploy`, creating the Unity Catalog connection, or starting
  ingestion -- there is no script here that does any of that, by design (see
  `references/approval-gate.md`). A human (or a separately authorized CI/CD process) takes the
  generated resources from here.
- Checking whether data landed by a deployed connector is correct -- that's **`data-quality`**
  (single object) or **`data-validation`** (source vs. target), and only after a human has actually
  deployed and run the connector.

## Reference material

- `references/connector-type-mapping.md` -- the source-system-to-connector-type table, why
  `source_system` is named explicitly rather than parsed from the contract, and how to extend it.
- `references/asset-bundle-resources.md` -- exactly what `uc_connection.yml` and
  `ingestion_pipeline.yml` contain, why the UC connection is documented rather than asserted as a
  native bundle resource, and how `table_configuration.primary_keys` is derived.
- `references/approval-gate.md` -- the two separate approvals (the source manifest's, and this
  skill's own), and why this skill matches the toolkit's existing "generate, never deploy"
  boundary rather than departing from it.
- `references/toolkit-conventions.md` -- cross-cutting rules shared by all six skills.
