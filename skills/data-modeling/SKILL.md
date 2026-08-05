---
name: data-modeling
description: >-
  Designs a Kimball-style dimensional model (facts, dimensions, grain, measures, SCD types,
  conformance) from business context a human provides, grounded against real silver-layer source
  objects. Produces model-spec.json plus a human-readable design_rationale.md -- input to
  data-discovery's resolution mode, which resolves this design against real objects and produces
  the data-contract.json that data-pipeline actually builds from. Gold-layer only: this skill
  verifies a usable curated silver layer exists for every object it would source from and refuses
  to design against bronze/raw or uncurated silver, distinctly from checking whether that layer's
  DATA is clean (which is data-quality/data-validation's job, not this one's). Use this at the
  start of a new star schema or when extending one with a new fact or dimension. Do NOT use this
  to map a design's fields to real physical columns (that's data-discovery in resolution mode,
  which this skill's output feeds) or to generate pipeline code (that's data-pipeline, which
  consumes a data-contract, never a model-spec directly). Do NOT use this to check whether
  existing gold-layer data is correct (that's data-quality/data-validation).
version: 1.1.0
---

# data-modeling

This skill ships as part of the `data-ai-skill-toolkit` plugin. Commands below use
`${CLAUDE_PLUGIN_ROOT}` for the toolkit-root shared paths (`scripts/`, `contracts/`) and
`${CLAUDE_SKILL_DIR}` for files bundled with this skill specifically -- both resolve correctly
regardless of the working directory or which repo this plugin is installed into. See the
top-level `README.md` "Installing as a plugin" for how this toolkit is packaged.

## Read/write boundary

**Read-only against client systems, same as data-discovery/data-quality/data-validation.** This
skill profiles silver-layer objects (`information_schema`/`DESCRIBE`-equivalent metadata, bounded
uniqueness checks for grain validation, table listings for SCD/conformance signals) and never
issues DDL or DML. Its only writes are `model-spec.json` and `design_rationale.md` into
`output_dir` -- see `references/toolkit-conventions.md` #1 and #3.

## What this produces

One `model-spec.json` (schema: `contracts/model-spec.schema.json`) plus one `design_rationale.md`
(referenced by `design_rationale_ref`) per run. A human review gate sits between this artifact and
`data-discovery`'s resolution mode -- see `references/toolkit-conventions.md` #7 gate 2: dimensional
design decisions (grain, SCD type, conformance) are business decisions wearing a technical hat,
and discovery should resolve an *approved* design, not a draft one.

## Workflow

1. **Gather business context, or ask for it.** `business_context.use_cases` and
   `business_context.business_questions` cannot come from scanning data -- they're what the star
   schema is FOR. If the human's request doesn't already state them clearly, ask (per
   `references/toolkit-conventions.md` #6's "Ask" behavior) rather than inventing plausible-sounding
   ones. A model designed against invented business questions is worse than a model that took one
   more turn to gather real ones.

2. **Identify every silver-layer object this model would source from**, then run the cost gate and
   silver-layer verification together:
   ```
   python "${CLAUDE_SKILL_DIR}/scripts/build_model_findings.py" \
     --lakehouse-dir <dir> --source-object <schema.table> [--source-object ... repeatable] \
     --dimension-table <schema.table> [repeatable -- objects to check for SCD history evidence] \
     --proposed-dimension-name <name> [repeatable -- for conformance candidate discovery] \
     --out model_findings.json
   ```
   **If this halts with `reason: silver_verification_failed`**: do not design a single fact or
   dimension against the refused object(s). See `references/silver-verification.md` for exactly
   what's being checked (curation structure, not data quality) and the one sanctioned way to
   proceed anyway (an explicit human override, `--force`, recorded prominently in `assumptions[]`
   -- never silently). Assemble a `model-spec.json` with `silver_verification.verified: false`,
   `reason_if_not_verified` populated, and empty `facts`/`dimensions` arrays rather than skipping
   artifact production entirely -- an auditable "we checked and refused" beats a bare halt with no
   record.
   **If this halts with `reason: cost_threshold_exceeded`**: same halt-and-ask behavior as every
   other skill -- report the estimate, do not pass `--force` without an explicit go-ahead in this
   conversation.

3. **For each candidate fact**, confirm the proposed grain actually holds (findings' `fact_grain_checks`,
   or run `${CLAUDE_SKILL_DIR}/scripts/validate_grain_against_measures.py` directly for a grain you're iterating on) before
   writing `grain.validated_against_measures: true` -- that field means every measure was actually
   checked to be well-defined at exactly this grain, not that grain uniqueness merely looks
   plausible. Classify each measure's `additivity` yourself (this is judgment, not something a SQL
   type answers -- see `references/kimball-concepts.md`), grounded in the measure's declared type
   from the findings (e.g. a measure declared `TEXT` needs a documented cast in its
   `source_to_target_mappings.transformation`, not a silent assumption it's already numeric).
   **If this fact's attributes genuinely come from more than one silver object** (e.g. a header
   table's attributes rolled 1:many down to the fact's own line-item grain, plus a dimension
   surrogate-key lookup), design the correct grain and mappings first, then declare
   `facts[].source_joins` -- don't compromise the grain or drop attributes just because a single
   source object would be simpler to specify. See `references/kimball-concepts.md`'s "Multi-source
   facts and dimensions" section for the equality-only join shape this needs and why.

4. **For each dimension**, review `scd_candidates` (sibling history-table evidence) before setting
   `scd_type`/`scd_rationale` per attribute -- see `references/scd-type-selection.md`. Review
   `conformance_candidates` before deciding `kind: conformed` and reusing an existing
   `conformance_group` versus designing a new `local` dimension -- see
   `references/conformed-dimensions.md`. Identify degenerate dimensions (operational identifiers
   that belong on the fact itself, not a separate table) and junk/bridge dimensions per the same
   reference. **A denormalized/snowflaked dimension** (product + product category + product
   model, a category self-joined once more for a parent category, etc.) declares
   `dimensions[].source_joins` the same way -- see `references/kimball-concepts.md`.

5. **Assemble `model-spec.json`** matching `contracts/model-spec.schema.json`, then validate:
   ```
   python "${CLAUDE_PLUGIN_ROOT}/scripts/validate_artifact.py" <output>/model-spec.json --schema-type model-spec --supported-major 1
   ```
   Do not declare the run successful until this passes.

6. **Write `design_rationale.md`** alongside it -- a human-readable explanation of the star schema
   (why this grain, why these conformed dimensions, why these SCD choices) for the business
   stakeholder review this design needs before discovery resolves it. Set
   `model-spec.json`'s `design_rationale_ref` to its path.

7. **If a previous run exists for this `model_id`**, diff against it (this schema type isn't yet
   registered in `scripts/diff_artifact.py`'s `DIFFERS` -- if you need a diff, compare the two
   `facts[]`/`dimensions[]` arrays by name manually and note material changes in your report; see
   `DECISIONS.md` for why this was left for a future phase rather than guessed at now).

8. **Report to the human, and stop at the review gate.** Summarize the star schema in plain
   language, flag `silver_verification` results for every source object, and be explicit that this
   design needs approval before `data-discovery` resolves it against real objects -- this skill
   does not proceed to discovery itself.

## When NOT to use this skill

- Mapping a model's fields to real physical source columns -- that's **`data-discovery`** in
  resolution mode, which consumes this skill's `model-spec.json` as input.
- Generating pipeline code -- that's **`data-pipeline`**, which consumes a `data-contract.json`
  (discovery's output), never a `model-spec.json` directly.
- Checking whether existing gold-layer data is correct -- that's **`data-quality`** (single
  object) or **`data-validation`** (source vs. target).
- Designing against an object this skill's own `silver_verification` check refuses -- see step 2.
  This isn't a routing decision to another skill; it's this skill declining to proceed until the
  source layer is actually curated (or a human explicitly overrides, informed of exactly what
  wasn't verified).

## Reference material

- `references/silver-verification.md` -- the five curation signals, why they check structure and
  not data quality, and the override path.
- `references/kimball-concepts.md` -- grain discipline, measure additivity, degenerate/junk/bridge
  dimensions.
- `references/scd-type-selection.md` -- SCD 0/1/2/3 decision guide and how history-table evidence
  feeds in.
- `references/conformed-dimensions.md` -- how conformance candidates are surfaced and confirmed.
- `references/toolkit-conventions.md` -- cross-cutting rules shared by all six skills.
