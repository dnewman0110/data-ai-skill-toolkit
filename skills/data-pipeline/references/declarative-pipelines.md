# Declarative pipeline modality (Lakeflow Declarative Pipelines / Spark Declarative Pipelines)

## When this is the default

Any target with `transform_complexity: simple_declarative` and `declarative_pipeline: true` in
`toolkit.yaml`'s `environment` block, that isn't a Lakeflow Connect case. This is most
silver->gold and bronze->silver medallion reshaping in a typical engagement -- straight column
mappings, joins expressible as a `SELECT`, and tests that map cleanly onto expectations.

## What the generated files contain

Two files per target: `declarative_pipeline.py` (the pipeline definition: a staging view reading
the source as a stream, a target streaming table, and an `apply_changes` call keyed on
`merge_keys`) and `expectations.py` (the `EXPECTATIONS` dict derived from the contract's
`uniqueness`/`nullability` tests -- see `contract-derived expectations` below).

## Why idempotency is largely structural here, not something the template implements

`apply_changes` (the declarative CDC/upsert primitive) is idempotent by design: it tracks its own
checkpoint and applies changes keyed on `keys=`, so re-running the pipeline against unchanged
source data recomputes to the same state rather than double-applying anything. This is the main
reason `references/decision-rubric.md` biases toward this modality as the default over
`pyspark_notebook` when either would technically work -- there's less for a human (or this
toolkit) to get wrong.

## `sequence_by` -- the one thing the template cannot safely infer

`apply_changes` requires a `sequence_by` column: something that monotonically increases with each
update to a given key, so out-of-order change events apply in the right order. A `data-contract.json`
has no field for "which column tracks recency" -- discovery doesn't currently profile for it. The
generated file uses the first merge key as a placeholder and leaves an explicit `# TODO (human
review required before deployment)` comment plus an `assumptions[]` entry in the manifest. **This
is a genuine, documented gap, not a silent guess** -- see `DECISIONS.md`. If the source has an
`updated_at`/`_ingested_at`/similar column, a human should point `sequence_by` at it before this
code is deployed.

## Contract-derived expectations

Only `uniqueness` and `nullability` tests are auto-rendered:

- `uniqueness:col1,col2` -> `expect_or_drop('valid_grain', 'col1 IS NOT NULL AND col2 IS NOT NULL')`.
  This is a non-null check on the key columns, not true duplicate detection -- actual dedup comes
  from `apply_changes`' own key-based upsert semantics, not a row-level expectation.
- `nullability:col` with `max_null_rate: 0` -> `expect_or_fail('col_not_null', 'col IS NOT NULL')`.
  A nonzero `max_null_rate` is a RATE threshold across a batch, not a per-row predicate an
  expectation can express -- it's recorded as "not carried forward" in `tests_carried_forward`,
  pointing at `data-quality` as the right tool for that check post-load.

`referential`, `range`, and `freshness` tests are never auto-rendered into expectations in this
toolkit version -- a referential expectation needs a subquery against the referenced table and a
freshness expectation needs a materialized notion of "now," both of which are judgment calls this
generator deliberately does not template-guess. `tests_carried_forward` records each as "not
carried forward" with the reason, rather than silently dropping it. Run `data-quality` against the
target after it's deployed and loaded to cover these.

## Deployment

Generating these files does not create a pipeline in Databricks. A human (or a separate,
authorized process) adds them to an actual Declarative Pipeline definition and runs/schedules it
-- see `toolkit-conventions.md` #1 and #7.
