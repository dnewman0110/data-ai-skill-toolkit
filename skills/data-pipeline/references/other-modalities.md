# Documented gaps: what this version of data-pipeline does NOT generate

Per `toolkit-conventions.md` #6 (never silently infer): the two gaps below are real limitations of
this skill's current scripts, not things it pretends to handle. Both surface explicitly --
`build_transform_spec.py` raises rather than guessing, and `recommend_modality.py` routes toward
`pyspark_notebook` (the only modality expressive enough for either case) so a human can complete
the work by hand, informed by what this skill COULD derive automatically.

## Multi-source joins -- what's supported, and what's still a real gap

`build_transform_spec.py` renders a real multi-table join when a target's `data-contract.json`
declares `table.source_joins`: a structured, equality-only join (a driving/grain object plus one
or more joined objects, each with an explicit `join_type` and column-equality `on` conditions --
see `contracts/data-contract.schema.json`). This covers genuine many-to-one lookup joins --
denormalizing a snowflaked dimension, rolling a header table's attributes down to a fact's own
grain, a dimension surrogate-key lookup -- and classifies as `transform_complexity:
simple_declarative`, routing to `declarative_pipeline` like any other reshape. See
`references/decision-rubric.md`'s worked example and `references/declarative-pipelines.md`'s
"Multi-source joins" section for exactly what gets rendered.

**Still a real gap, still refused outright, never guessed at:**

- **No `source_joins` declared at all**, even though a target's columns map from more than one
  source object. `build_transform_spec.py` raises `ValueError` naming the target and the distinct
  source objects found rather than silently picking one or attempting to synthesize a join
  condition it was never told. Classify as `transform_complexity: complex_procedural`, which routes
  to `pyspark_notebook`. A human then writes the join, starting from the per-column source mappings
  the failed `build_transform_spec.py` call already surfaced (rerun it per source object to get
  each half's mapping) as a reference, not a template.
- **A join that genuinely can't be expressed as an equality condition** -- a range/`BETWEEN`
  predicate, a join that would fan out the driving table's rows (one-to-many in the wrong
  direction), or one that needs aggregation to resolve. `source_joins` has no field for any of
  these by design (not an oversight to fix later) -- forcing an approximate equality condition
  onto a genuinely non-equality relationship would silently produce wrong results, exactly what
  `toolkit-conventions.md` #6 rules out. These stay `complex_procedural`/`pyspark_notebook`.
- **Local idempotency proof for a multi-source target.** `validate_pipeline_locally.py` reports
  `not_applicable` for any spec with `is_multi_source: true` -- `derive_mock_data.py` synthesizes
  one flat mock table per target, with no notion of multiple mock source tables sharing real
  foreign-key relationships across aliases, so a local join-based proof would either crash or
  "prove" idempotency by joining everything to NULL. The REAL generated Spark code still renders
  the full multi-table join correctly; only this local, mock-data-based proof doesn't yet cover it.
  See `references/idempotency-and-mock-data.md`.

## `streaming_cdc` load pattern

`contracts/pipeline-manifest.schema.json`'s `load_pattern` enum includes `streaming_cdc` for
low-latency/continuous targets, but neither the PySpark notebook nor the Declarative Pipeline
template in this toolkit version generates true low-latency streaming logic (structured streaming
with a short trigger interval and a durable checkpoint location, or a Declarative Pipeline
streaming table with a live/continuous refresh policy) -- `generate_pipeline_code.py` only ever
produces `merge_upsert` (batch MERGE / batch `apply_changes`) or `full_refresh` code, regardless
of `requires_streaming`. If a target needs true streaming, classify it `complex_procedural`
(routes to `pyspark_notebook`) and hand-extend the generated batch shell with a
`.readStream`/`.writeStream` and an explicit `checkpointLocation` -- see the restart-semantics
discussion in `references/pyspark-notebook.md` for what a checkpoint buys you that this toolkit's
generated batch code does not need. This is scoped out of v1 deliberately rather than generated
half-correctly; see `DECISIONS.md`.
