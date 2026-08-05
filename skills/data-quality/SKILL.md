---
name: data-quality
description: >-
  Scans a SINGLE data object against a configurable set of deterministic quality checks --
  row counts, null rates, uniqueness, value ranges, referential integrity, freshness, and
  hand-written SQL predicates -- and diagnoses every failed or warned check with a labeled,
  confidence-scored LLM root-cause explanation. Checks can be hand-authored per run/in
  toolkit.yaml, or derived directly from a data-contract.json's tests[] so discovery's proposed
  tests actually get run on a schedule rather than just sitting in a contract. Use this whenever
  a consultant needs to know "is this table's data actually clean" -- recurring monitoring, a
  one-off spot check before a handoff, or turning a contract's proposed tests into a real
  scheduled scan. Produces quality-report.json. Do NOT use this when there are two objects to
  compare against each other (source vs. target after a pipeline runs) -- that's
  data-validation, which this skill never performs (no second object, ever). Do NOT use this to
  propose what tests should exist in the first place -- that's data-discovery. This skill never
  writes to client systems and never applies a suggested fix.
version: 1.0.0
---

# data-quality

This skill ships as part of the `data-ai-skill-toolkit` plugin. Commands below use
`${CLAUDE_PLUGIN_ROOT}` for the toolkit-root shared paths (`scripts/`, `contracts/`) and
`${CLAUDE_SKILL_DIR}` for files bundled with this skill specifically -- both resolve correctly
regardless of the working directory or which repo this plugin is installed into. See the
top-level `README.md` "Installing as a plugin" for how this toolkit is packaged.

## Read/write boundary

**Read-only against client systems. Always.** This skill scans one object (`SELECT`,
`information_schema`/`DESCRIBE`, bounded profiling queries, and -- for `custom_sql` checks only --
a caller-supplied `SELECT` statement guarded by `scripts/lakehouse_adapter.py`'s
`assert_read_only_select`) and never issues DDL or DML. That guard is defense-in-depth, not the
only line of defense -- the toolkit.yaml connection this skill runs under should itself hold a
read-only credential; see `references/toolkit-conventions.md` #1 and #2. Nothing this skill finds
is ever remediated by this skill -- `diagnosis.suggested_fix` is always a proposal for a human,
never an action taken.

## What this produces

One `quality-report.json` (schema: `contracts/quality-report.schema.json`) per target object per
run. Every failed or warned check gets a diagnosis; a human decides what, if anything, to do about
it -- this skill's job ends at "here's what's wrong and why I think so," the same standing rule
`data-validation` follows for its own suggested fixes.

## Workflow

1. **Assemble the check list.** Two sources, and they merge (hand-authored wins on a `check_id`
   collision -- an explicit override is deliberate):
   - **Hand-authored**: from `toolkit.yaml`'s quality config or a per-run checks file -- see
     `references/check-types-and-thresholds.md` for the definition format per check type.
   - **Contract-derived**: from a `data-contract.json`'s `tables[].tests[]`, via
     `${CLAUDE_SKILL_DIR}/scripts/derive_checks_from_contract.py`. This is the explicit
     integration point with `data-discovery` the toolkit is built around -- a contract's proposed
     tests should actually get run, not just sit in a JSON file. See
     `references/contract-derived-checks.md` for the type mapping and what `derived_from_contract_test`
     records.

   **If a `data-contract.json` already exists for the object, derive from it rather than
   hand-authoring from scratch**, even for columns that feel obvious. Discovery's profiling
   already caught nuances a fresh-eyes check list can plausibly miss -- e.g. a column with no
   `NOT NULL` constraint but a suspicious nonzero null rate reads as "safe to skip" if you're only
   checking declared-required columns, but discovery already flagged it and proposed a test with a
   defensible threshold. Hand-author checks to fill genuine gaps (a business rule discovery
   couldn't know about) or when no contract exists yet, not as the default starting point when one
   does.

2. **Run the cost gate, then scan.** Run
   `${CLAUDE_SKILL_DIR}/scripts/build_quality_findings.py` with `--schema`/`--table` for the
   target object, `--checks-json` and/or `--contract-json`/`--contract-table` for the check list,
   and `--max-rows-scanned`/`--max-bytes-scanned` from `toolkit.yaml`. This one command runs the
   pre-flight cost estimate and **halts with exit code 1 and no checks run** if it exceeds
   threshold -- same halt-and-ask behavior as `data-discovery` and `data-validation`; never pass
   `--force` without an explicit go-ahead in this conversation.

3. **Read `quality_findings.json`.** For every check: `status` (`passed` / `failed` / `warned` /
   `not_evaluated`), `measured_value`, `threshold`, and (when `not_evaluated`)
   `reason_not_evaluated` naming exactly why the check couldn't run (missing column, malformed
   params, a `custom_sql` execution error, no non-null values to check a range against). Every
   status here is measured, not guessed -- see `references/check-types-and-thresholds.md` for
   exactly how each check type's status is computed.

4. **Diagnose every `failed` or `warned` check.** This is your job, not a script's (see
   `references/toolkit-conventions.md` #5). For each: form a root-cause hypothesis from the check
   definition, the measured value vs. threshold, and any available context (the object's other
   check results, declared constraints, comments) -- not just "this failed" but *why*. Write
   `diagnosis.root_cause`, `diagnosis.confidence` (per `contracts/confidence-rubric.md`),
   `diagnosis.basis` (what you actually checked, not just what seems plausible), and
   `diagnosis.suggested_fix` (phrased as a proposal, never as something you're about to do). Set
   `diagnosis.source` to `"llm_inferred"` always. Checks with status `passed` or `not_evaluated`
   get no diagnosis entry -- there's nothing to explain about a check that passed, and a check
   that couldn't run has no result to diagnose, only a `reason_not_evaluated` that's already
   self-explanatory.

5. **Assemble `quality-report.json`** matching `contracts/quality-report.schema.json`
   (`checks[]` straight from findings, `diagnoses[]` from step 4, `summary` counts), then validate:
   ```
   python "${CLAUDE_PLUGIN_ROOT}/scripts/validate_artifact.py" <output>/quality-report.json --schema-type quality-report --supported-major 1
   ```
   Do not declare the run successful until this passes.

6. **If a previous run exists for this object**, diff against it:
   ```
   python "${CLAUDE_PLUGIN_ROOT}/scripts/diff_artifact.py" <previous>/quality-report.json <new>/quality-report.json --schema-type quality-report --out <new>/quality-report.diff.json
   ```
   This surfaces exactly which checks changed status since last time -- newly failing, newly
   passing, newly not-evaluated -- rather than making the user compare two full reports by eye.
   Never overwrite the previous artifact.

7. **Report to the human.** Summarize: how many checks passed/failed/warned/were not evaluated,
   every failed/warned check's diagnosis and confidence, and the reminder that suggested fixes are
   proposals -- this skill applied none of them.

## When NOT to use this skill

- Comparing a source and a target to see if they match -- that's **`data-validation`**. This
  skill only ever looks at ONE object; if there's a second object in the picture at all
  (a pipeline's source, a migration's before-state, anything to diff against), the right skill is
  `data-validation`, not a pair of quality scans a human has to compare by hand.
- Proposing what checks/tests SHOULD exist for an object in the first place -- that's
  **`data-discovery`**, whose contract `tests[]` this skill can run (see step 1) but does not
  invent.
- Generating or fixing the pipeline code that produced the object being scanned -- that's
  **`data-pipeline`**, and even after this skill diagnoses a failure, a human decides what, if
  anything, changes in that code.

## Reference material

- `references/check-types-and-thresholds.md` -- the check definition format, all seven check
  types, exactly how `status` is computed for each, and what makes a check `not_evaluated`.
- `references/contract-derived-checks.md` -- the contract-test-to-quality-check type mapping and
  how derived and hand-authored checks merge.
- `references/toolkit-conventions.md` -- cross-cutting rules shared by all six skills.
