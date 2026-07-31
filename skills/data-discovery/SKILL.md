---
name: data-discovery
description: >-
  Converts business intent OR an approved data-modeling model-spec into a machine-readable data
  contract (mappings, grain, tests, redacted samples, confidence-scored source-to-target
  mappings), grounded in real profiled data from the client's lakehouse -- never invented from
  column names alone. Use this whenever a consultant needs to figure out what data actually
  exists for a business need, or whether the star schema data-modeling designed actually resolves
  against real tables. Two modes: greenfield (prose business intent in, broad exploration) and
  resolution (a model-spec artifact in, narrow targeted resolution that reports anything it can't
  satisfy rather than dropping it). Produces data-contract.json, consumed by data-pipeline. Do NOT
  use this for designing a dimensional model from business questions (that's data-modeling, whose
  output this skill's resolution mode consumes), for generating pipeline code from a contract
  (that's data-pipeline), or for scanning/diffing data already in production (that's data-quality
  and data-validation). This skill never writes to client systems.
version: 1.0.0
---

# data-discovery

This skill ships as part of the `data-ai-skill-toolkit` plugin. Commands below use
`${CLAUDE_PLUGIN_ROOT}` for the toolkit-root shared paths (`scripts/`, `contracts/`) and
`${CLAUDE_SKILL_DIR}` for files bundled with this skill specifically -- both resolve correctly
regardless of the working directory or which repo this plugin is installed into. See the
top-level `README.md` "Installing as a plugin" for how this toolkit is packaged.

## Read/write boundary

**Read-only against client systems. Always.** This skill explores and profiles source objects
(`SELECT`, `information_schema`/`DESCRIBE`, sampled queries) and never issues DDL or DML against
anything in the client's catalog. Everything it produces is written to the configured
`output_dir`, outside the toolkit repo and outside the client's warehouse. Any temp objects a
profiling step needs go in the configured `scratch_schema`, named with the run ID, dropped when
the run ends. See `references/toolkit-conventions.md` #1 -- this is toolkit-wide, not special to
this skill, but it's restated here because discovery is the skill most tempted to "just create a
quick view to check this join," and that temptation is exactly what the scratch-schema rule
exists for.

## What this produces

One `data-contract.json` (schema: `contracts/data-contract.schema.json`) per invocation, covering
every target table requested. **Human review gate**: a contract with any `llm_inferred` mapping
is a proposal, not a spec -- hand it to a human before `data-pipeline` touches it. Say this
explicitly when you finish a run.

## Two invocation modes

- **Greenfield**: input is prose business intent ("we need order-level revenue by region").
  Explore broadly for plausible candidate source tables/columns; every mapping you propose from
  naming/structure alone is `mapping_type: llm_inferred` with a confidence score.
- **Resolution**: input is a `model-spec.json` from `data-modeling`. Resolve its facts/dimensions
  against real objects narrowly and specifically -- don't explore broadly, don't second-guess the
  design. Anything in the spec you cannot satisfy against real data goes in
  `unresolved_requirements[]`, never silently dropped.

Full detail, including how to tell which mode you're in and what "narrow" means concretely, is in
`references/invocation-modes.md`. Read it before your first run of either mode.

## Workflow

1. **Identify targets.** Greenfield: from the business intent, name candidate schema.table
   objects to explore (start from `list_tables` on the schemas the target catalog config points
   at). Resolution: the target objects are implied by the model-spec's `source_to_target_mappings`
   and dimension `source_mapping` fields -- go straight to those, don't explore beyond them unless
   a mapping is missing entirely.

2. **Run the cost gate, then profile.** Run
   `${CLAUDE_SKILL_DIR}/scripts/build_findings.py` with `--target schema.table` for every
   target object, plus `--backend`/`--catalog`/`--lakehouse-dir` from what `toolkit.yaml`'s
   `environment` block resolves: `--backend sqlite_fixture --lakehouse-dir <dir>` against the
   fixture lakehouse in evals, or `--backend databricks_connect --catalog <name>` in production,
   which uses `DatabricksConnectAdapter` in `scripts/lakehouse_adapter.py` -- an already-
   authenticated Databricks Connect session in this environment, no separate credentials passed
   in. Also pass `--max-rows-scanned` /
   `--max-bytes-scanned` from `toolkit.yaml`'s `cost_and_blast_radius` block and
   `--sensitive-columns-json` from its `sample_data.sensitive_columns`. This one command runs the
   pre-flight cost estimate, and **halts with exit code 1 and no profiling done** if the estimate
   exceeds threshold. If it halts: stop, show the user the printed cost decision, and ask before
   re-running with `--force` -- never pass `--force` without an explicit go-ahead in this
   conversation. See `references/profiling-and-cost-bounds.md` for sampling strategy and what
   "bounded" means for each check.

3. **Read `discovery_findings.json`.** This is the deterministic half: for every target, declared
   constraints, profiled column stats, candidate-key uniqueness results, FK orphan checks,
   proposed tests with their `threshold_basis`, plain-language `findings[]` (measured facts worth
   a human's attention -- a violated candidate key, an orphaned FK, a null rate that looks
   accidental, a TEXT column that's numeric under the hood), and redacted sample records. Nothing
   in this file required judgment to produce.

4. **Do the interpretation.** This is your job, not a script's (see
   `references/toolkit-conventions.md` #5 -- the deterministic/LLM boundary). For each target
   table:
   - **Determine grain.** Use the findings' `candidate_keys`: a `declared_primary_key` entry that
     `is_unique` is your grain at `explicit_constraint`/`profiled_unique_key` confidence tier
     (see `references/grain-and-tests.md`). If nothing profiled unique, you may reason from
     naming/business-intent context, but that's `llm_inference_from_naming` -- score it in the
     0.2-0.49 band per `contracts/confidence-rubric.md` and say why. If you genuinely can't
     support any grain statement with evidence, **halt** -- do not ship a contract with a guessed
     grain. This is the one failure mode this skill treats as non-negotiable: a contract with a
     wrong grain silently corrupts everything built on it downstream.
   - **Map columns** (greenfield) or **resolve required fields** (resolution). Greenfield: for
     each target concept, look for an `explicit_alias` (exact name match with corroborating
     comment) or `name_and_type_match` in the findings; where you're inferring from naming alone,
     set `mapping_type: llm_inferred` with confidence + basis. Resolution: match the model-spec's
     `source_to_target_mappings` against the findings' columns; anything not found becomes an
     `unresolved_requirements[]` entry with a reason, never a silent drop.
   - **Carry `source_to_target_mappings[].transformation` through into `source.transformation`**
     (resolution mode) -- verbatim, translating the `"direct"` sentinel to `null`. A model-spec's
     `CAST(...)`/`DATEDIFF(...)`/etc. is a reviewed design decision, not something to collapse to a
     bare column reference just because `data-contract.schema.json`'s `source.column` only takes a
     single name -- `source.transformation` is exactly the field for this; `data-pipeline` renders
     it verbatim and does not re-derive it. Likewise carry a resolved dimension attribute's
     `scd_type` through into the column's `scd_type`.
   - **Set `source.source_type` from the findings' `declared_type`, in both modes.** This is
     already-measured data (`profile_object.py` computed it; you're not inferring anything) --
     it's what lets `data-pipeline` catch a declared target type that doesn't actually match the
     source and no `transformation` was given, instead of silently rendering a bare alias that's
     wrong. Set it even when the mapping is a confident `explicit_alias` with no type concerns --
     the check downstream is mechanical, not a comment on this mapping's confidence.
   - **Carry every `findings[]` entry into `assumptions[]`** in the final contract, verbatim or
     lightly reworded -- these are exactly the kind of thing `references/toolkit-conventions.md`
     #6 means by "never silently infer."
   - **Copy `proposed_tests` into each table's `tests[]`** as-is; they're already schema-shaped
     and already have a defensible `threshold_basis`.

5. **Assemble `data-contract.json`** matching `contracts/data-contract.schema.json`, then validate
   it:
   ```
   python "${CLAUDE_PLUGIN_ROOT}/scripts/validate_artifact.py" <output>/data-contract.json --schema-type data-contract --supported-major 1
   ```
   Do not declare the run successful until this passes. If it fails, fix the artifact -- don't
   loosen the schema and don't hand-wave the error to the user as "probably fine."

6. **If a previous run exists for this contract** (same `contract_id` in `output_dir`), diff
   against it and write the diff alongside the new artifact:
   ```
   python "${CLAUDE_PLUGIN_ROOT}/scripts/diff_artifact.py" <previous>/data-contract.json <new>/data-contract.json --schema-type data-contract --out <new>/data-contract.diff.json
   ```
   Never overwrite the previous artifact. If `material_change` is `false`, say so plainly instead
   of walking the user through a diff with nothing in it.

7. **Report to the human.** Summarize: what was profiled, the grain determination and its
   confidence, every `llm_inferred` mapping and its confidence, every `unresolved_requirements`
   entry (resolution mode), and the human review gate reminder from "What this produces" above.

## When NOT to use this skill

- Designing the target star schema from business questions -- that's **`data-modeling`**. Its
  output (`model-spec.json`) is this skill's resolution-mode input; don't reimplement dimensional
  design decisions (grain, SCD type, conformance) here.
- Generating pipeline code from an approved contract -- that's **`data-pipeline`**. This skill's
  job ends at a validated `data-contract.json`; it never writes code.
- Running quality checks or root-cause diagnosis against an object already in production on a
  recurring basis -- that's **`data-quality`**. Discovery's tests[] proposals are a starting point
  for quality's checks, not a replacement for running them.
- Comparing a source and target for correctness after a pipeline exists -- that's
  **`data-validation`**. Discovery never compares two objects to each other; it profiles one at a
  time.

## Reference material

- `references/invocation-modes.md` -- greenfield vs. resolution in detail, with examples.
- `references/grain-and-tests.md` -- grain determination tiers and the deterministic test-proposal
  rules (nullability, uniqueness, referential, range, freshness), matched to
  `contracts/confidence-rubric.md`.
- `references/profiling-and-cost-bounds.md` -- sampling strategy, row caps, what the cost gate
  checks and what to do when it says no.
- `references/toolkit-conventions.md` -- cross-cutting rules shared by all five skills (read/write
  boundaries, secrets, client data isolation, cost gates, the deterministic/LLM boundary, human
  review gates, idempotency). Read this if you haven't already; it's referenced above throughout
  rather than restated.
