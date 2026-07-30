# Documented gaps: what this version of data-pipeline does NOT generate

Per `toolkit-conventions.md` #6 (never silently infer): the two gaps below are real limitations of
this skill's current scripts, not things it pretends to handle. Both surface explicitly --
`build_transform_spec.py` raises rather than guessing, and `recommend_modality.py` routes toward
`pyspark_notebook` (the only modality expressive enough for either case) so a human can complete
the work by hand, informed by what this skill COULD derive automatically.

## Multi-source joins

`build_transform_spec.py` requires every column in a target table to map from a single source
object; it raises `ValueError` naming the target and the distinct source objects found rather than
silently picking one or attempting to synthesize a join. If a target genuinely needs a join,
classify it as `transform_complexity: complex_procedural` (see `references/decision-rubric.md`),
which routes to `pyspark_notebook`. A human then writes the join, starting from the per-column
source mappings the failed `build_transform_spec.py` call already surfaced (rerun it per source
object to get each half's mapping) as a reference, not a template.

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
