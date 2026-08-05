---
name: data-validation
description: >-
  Compares a source and a target object row-for-row and column-for-column to find and diagnose
  real discrepancies -- never a scan of one object alone. Staged and deterministic: row counts,
  then a full content hash, then per-column aggregates, then a bounded row-level diff, digging
  deeper only when a shallower stage found something. Root-cause diagnosis is a labeled LLM
  inference with a confidence score; suggested fixes are suggestions only -- this skill never
  applies a remediation. Use this whenever a consultant needs to know "does the target actually
  match the source" after a pipeline runs, after a migration, after a backfill, or on a recurring
  post-load check -- not to check whether one object's data quality is acceptable on its own (no
  second object to compare against). Consumes a data-contract.json or a hand-specified
  source/target pair; produces validation-report.json. Do NOT use this for scanning a single
  object against configurable assertions with no comparison target (that's data-quality), for
  proposing source-to-target mappings (that's data-discovery), or for generating the pipeline
  code that produced the target in the first place (that's data-pipeline). This skill never
  writes to client systems and never remediates a discrepancy it finds.
version: 1.0.0
---

# data-validation

This skill ships as part of the `data-ai-skill-toolkit` plugin. Commands below use
`${CLAUDE_PLUGIN_ROOT}` for the toolkit-root shared paths (`scripts/`, `contracts/`) and
`${CLAUDE_SKILL_DIR}` for files bundled with this skill specifically -- both resolve correctly
regardless of the working directory or which repo this plugin is installed into. See the
top-level `README.md` "Installing as a plugin" for how this toolkit is packaged.

## Read/write boundary

**Read-only against client systems. Always.** This skill reads from both the source and target
objects (`SELECT`, `information_schema`/`DESCRIBE`, bounded fetches) and never issues DDL or DML
against either one. Discrepancies are found and diagnosed; they are never remediated by this
skill. Everything it produces is written to the configured `output_dir`. See
`references/toolkit-conventions.md` #1 -- this is toolkit-wide, restated here because "just fix
the row while I'm in there" is exactly the temptation a comparison tool with warehouse credentials
creates, and it's not this skill's call to make.

## What this produces

One `validation-report.json` (schema: `contracts/validation-report.schema.json`) per
source/target pair. **Human review gate**: after a validation report identifies a discrepancy,
a human reviews it *before any remediation is applied* -- this skill's `suggested_fix` fields are
proposals, never actions. Say this explicitly when you finish a run with any discrepancies.

## Workflow

1. **Identify source and target.** Either from a `data-contract.json`'s `tables[].source` (source)
   paired with the contract's own `target_catalog`/`target_schema`/`name` (target, once
   `data-pipeline` has built it), or from an explicit source/target pair the user names directly
   ("compare silver.orders against gold.legacy_fct_orders"). Identify the key columns to match rows
   by -- the contract's stated grain, or ask if none is available and it's not obvious from
   declared constraints.

2. **Run the cost gate, then compare.** Run
   `${CLAUDE_SKILL_DIR}/scripts/build_validation_findings.py` pointing `--source-*`/`--target-*`
   at what `toolkit.yaml` resolves for each side (they may be different connections -- this script
   accepts independent adapters per side; see `references/staged-comparison.md` for the
   cross-platform story), `--key-column` for each grain column, and the cost/redaction config from
   `toolkit.yaml` (`cost_and_blast_radius`, `validation.content_check_row_cap`,
   `validation.row_level_diff_row_cap`, `sample_data.sensitive_columns`,
   `validation.known_acceptable_differences`). This one command runs the pre-flight cost estimate
   against BOTH sides and **halts with exit code 1 and no comparison done** if either side's
   estimate exceeds threshold. If it halts: stop, show the user the printed cost decision, and ask
   before re-running with `--force`.

3. **Read `validation_findings.json`.** This is the deterministic half: which stage the comparison
   stopped at, and if it didn't stop clean, every discrepancy found -- `kind`
   (`missing_from_target` / `extra_in_target` / `changed`), the row `key`, `columns_affected`, and
   the (already redacted, already capped) `source_row`/`target_row` content. Nothing in this file
   required judgment to produce; see `references/staged-comparison.md` for exactly how each stage
   works and what "digging deeper only as needed" means concretely.

4. **Diagnose every discrepancy.** This is your job, not a script's (see
   `references/toolkit-conventions.md` #5). For each entry in `findings.comparison.discrepancies`:
   - Look at `columns_affected`, the redacted `source_row`/`target_row`, and (where available)
     declared FK lineage or comments from `contracts/data-contract.schema.json`-shaped context, to
     form a root-cause hypothesis -- not just "these differ" but *why* (a dropped join, a filter
     that's too aggressive, a timing/freshness gap, a type-coercion bug, etc).
   - Write `diagnosis.explanation`, `diagnosis.confidence` (per
     `contracts/confidence-rubric.md`), `diagnosis.basis` (what evidence grounded the diagnosis --
     name what you actually checked, not just what seems plausible), and `diagnosis.suggested_fix`
     (a proposal only -- phrase it as one, e.g. "confirm with the business whether X should happen,
     then..." not as an instruction you're about to carry out).
   - Set `diagnosis.source` to `"llm_inferred"` always -- there is no non-inferred diagnosis; the
     comparison that FOUND the discrepancy was deterministic, the explanation of *why* never is.
   - Map each finding's `kind`/`key`/`columns_affected` directly into the report's
     `discrepancies[]` entry; set `stage_detected` to `"row_level_diff"` (that's the stage this
     skill's discrepancies are always identified at -- see `references/staged-comparison.md`); set
     `sample_diff_ref` to `null` unless you separately wrote a larger detail file for a discrepancy
     whose full row content didn't fit inline.

5. **Assemble `validation-report.json`** matching `contracts/validation-report.schema.json`
   (`stages[]`, `normalization_applied`, `type_coercion_map` if source/target platforms differ,
   `known_acceptable_differences_excluded[]` straight from the findings, `summary`), then validate:
   ```
   python "${CLAUDE_PLUGIN_ROOT}/scripts/validate_artifact.py" <output>/validation-report.json --schema-type validation-report --supported-major 1
   ```
   Do not declare the run successful until this passes.

6. **If a previous run exists for this source/target pair**, diff against it:
   ```
   python "${CLAUDE_PLUGIN_ROOT}/scripts/diff_artifact.py" <previous>/validation-report.json <new>/validation-report.json --schema-type validation-report --out <new>/validation-report.diff.json
   ```
   This tells you (and the user) exactly which discrepancies are new, which were resolved since
   last time, and which persist unchanged -- never make the user eyeball two full reports to find
   that out. Never overwrite the previous artifact.

7. **Report to the human.** Summarize: how deep the comparison went and why (stopped clean at
   `hash_aggregate`, or found N discrepancies via `row_level_diff`), every discrepancy's diagnosis
   and confidence, any `known_acceptable_differences` that were excluded and why, and the human
   review gate reminder -- suggested fixes are proposals, this skill applies none of them.

## Known-acceptable differences

Some discrepancies are expected and declared acceptable (a known timing lag, a documented
intentional filter). Declare them in `toolkit.yaml`'s `validation.known_acceptable_differences` (or
pass an override at runtime) rather than re-discovering and re-explaining the same accepted gap on
every run. See `references/known-acceptable-differences.md` for the declaration format
(`column_ignore` / `key_ignore` rules) and why it's a small fixed rule language rather than
arbitrary code.

## When NOT to use this skill

- Checking whether a single object's data meets configurable assertions (null rates, ranges,
  uniqueness) with no second object to compare against -- that's **`data-quality`**. This skill
  never runs without both a source and a target.
- Proposing how source columns map to a target's shape in the first place -- that's
  **`data-discovery`**. This skill validates an existing source/target pair; it doesn't design one.
- Writing or fixing the code that produces the target -- that's **`data-pipeline`**, and even after
  this skill finds and diagnoses a discrepancy, the human review gate sits between that diagnosis
  and any code change.

## Reference material

- `references/staged-comparison.md` -- the four stages in detail: stopping criteria, what's
  pushed down vs. fetched, the content-check scale cap and its documented limitation, and the
  cross-platform/same-platform story.
- `references/normalization-and-type-coercion.md` -- how ordering, nulls, floats, and timezones
  are normalized before hashing, and how a source/target type mismatch (e.g. TEXT vs. numeric) is
  detected and coerced rather than reported as a false discrepancy.
- `references/known-acceptable-differences.md` -- the declaration format for expected/accepted
  discrepancies and how they're excluded from `discrepancies[]` without being silently forgotten
  (`known_acceptable_differences_excluded[]` still records them).
- `references/toolkit-conventions.md` -- cross-cutting rules shared by all six skills.
