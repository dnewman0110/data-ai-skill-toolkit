# Modality decision rubric

`scripts/recommend_modality.py` applies this rubric deterministically once the four factors below
are classified. Classifying them is the agent's job (per `toolkit-conventions.md` #5) -- this file
is worked examples to make that classification consistent across runs and across engagements.

## The four factors

### `source_is_managed_connector`

True only when BOTH: (a) the source object lives in a system Lakeflow Connect has a first-party
connector for (Salesforce, SQL Server, ServiceNow, Workday, SharePoint, and similar SaaS/DB
systems -- check the current Databricks docs for the live list, it grows over time), AND (b) no
transformation is needed at ingestion -- the data lands as-is into bronze.

- `acme_retail_dev.silver.orders` -> **false**. It's already inside the lakehouse, already
  curated silver -- there's no external system to connect to.
- A raw Salesforce `Opportunity` object landing into `bronze.salesforce_opportunity` with no
  reshaping -> **true**.
- The same Salesforce object, but the target also needs a join against a local reference table
  during ingestion -> **false** -- that join makes it not a pure landing operation; the value of
  Lakeflow Connect (managed CDC, managed schema evolution) doesn't extend to arbitrary
  transformation logic bolted on afterward. Land it first with Lakeflow Connect, then transform it
  in a separate declarative_pipeline or pyspark_notebook step.

### `requires_streaming`

True when the target needs continuous or low-latency refresh (seconds-to-minutes), not "run once a
day is fine." Most gold-layer analytical fact/dimension tables in a consultancy engagement are
**false** here -- batch is the common case. Don't default to true just because the source table
happens to be Delta (every table in this environment is Delta; that's not a streaming signal).

### `transform_complexity`

- `simple_declarative`: the target's `data-contract.json` columns are all straightforward mappings
  (rename, cast, simple expression) from a SINGLE source object, and its tests are the kind
  `data-quality`/Declarative Pipeline expectations already express (nullability, uniqueness,
  range, referential). This is the common case for medallion silver->gold reshaping.
- `complex_procedural`: the target needs multi-source joins, external API calls mid-pipeline,
  row-level branching that isn't expressible as a CASE expression, iterative/stateful logic, or a
  a business rule that genuinely needs imperative code to express correctly. If
  `build_transform_spec.py` refuses (multi-source), that alone is enough to classify this as
  `complex_procedural` -- don't try to force a single-source spec by picking one source and
  dropping the rest.

### `target_layer`

Read directly off the contract table's `target_schema` (`bronze` / `silver` / `gold`) -- this one
is close to measured, not really a judgment call, but it's still recorded here because it's an
input to the rule, not derived independently by `recommend_modality.py`.

## Why the rule is ordered the way it is (in `recommend_modality.py`)

1. **Availability gates everything first.** A modality disabled in `toolkit.yaml`'s
   `environment` block is never recommended, full stop -- there's no point recommending
   Declarative Pipelines to a team whose workspace/tier doesn't have it enabled, or a config a
   team has said they don't want.
2. **Lakeflow Connect is checked before complexity**, because it's the narrowest, most specific
   case (managed source AND bronze AND no reshaping) -- if it applies, it's almost always the
   right answer regardless of how "simple" the eventual reshaping downstream will be, because
   ingestion and transformation are different pipeline stages with different tools.
3. **Complexity is checked before defaulting to declarative**, because `pyspark_notebook` is the
   only modality that can express `complex_procedural` logic at all -- there's no fallback
   ordering issue here, it's a hard capability constraint.
4. **`declarative_pipeline` is the default**, not `pyspark_notebook`, for everything left over,
   because idempotency and restart semantics come largely for free with Declarative Pipelines
   (see `references/declarative-pipelines.md`) and a consultancy toolkit should bias toward the
   modality that's harder to get idempotency wrong in, when the transform doesn't force a choice.
