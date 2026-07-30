# PySpark notebook modality

## When this is the right (or forced) choice

- `transform_complexity: complex_procedural` -- multi-source joins, external calls, branching
  logic Declarative Pipelines can't express. This is the only modality that CAN express arbitrary
  procedural logic, so when the rubric lands here it isn't really optional.
- `declarative_pipeline` disabled in this engagement's `toolkit.yaml` `environment` block
  (older DBR, workspace tier without it enabled) -- the universal fallback.
- One-off backfills or migrations where a scheduled, always-on declarative pipeline is the wrong
  shape for a run-once operation.

## What the generated template does

`templates/pyspark_notebook.py.tmpl` renders a `spark.table(...).select(...)` reading the source,
followed by a branch on whether `merge_keys` is non-empty:

- **`merge_upsert`**: a `DeltaTable.merge(...).whenMatchedUpdateAll().whenNotMatchedInsertAll()`,
  keyed on `merge_keys`. This is a real Delta MERGE -- idempotent by construction against
  unchanged source rows, because re-applying the same source row to the same target row via
  `whenMatchedUpdateAll` just re-sets it to the same values.
- **`full_refresh`** (no merge keys): `df.write.mode("overwrite")`. Idempotent by atomic
  replacement, not by upsert -- a restart after a partial failure simply re-runs the same
  overwrite; there's no partial-apply state to worry about because Delta's overwrite is atomic.

## Restart semantics

Both branches are safe to re-run from the top after a failure with NO additional bookkeeping: the
merge is idempotent against unchanged source data, and the overwrite is atomic. This intentionally
keeps the generated notebook simple -- a hand-authored notebook with genuinely stateful,
multi-step logic (the `complex_procedural` case this modality exists for) needs its OWN
restart/checkpoint design that this template cannot generate for you; the template covers the
column-mapping-plus-load-pattern shell, not the procedural logic that justified choosing this
modality in the first place. That logic is exactly the part a human writes.

## What the template does not handle

- Streaming sources (`requires_streaming: true` with `complex_procedural` logic) -- this v1
  template is batch-only. See `references/other-modalities.md`.
- Multi-source joins -- `build_transform_spec.py` refuses these; a human starts from the
  single-source column mappings this skill CAN derive and adds the join by hand.
- Orchestration -- the notebook is generated, not scheduled. A human (or a separate, authorized
  process) creates the Databricks Job that runs it -- see `toolkit-conventions.md` #1 and #7.
