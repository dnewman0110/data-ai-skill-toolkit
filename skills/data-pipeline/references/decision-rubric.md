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
  (rename, cast, simple expression) from a SINGLE source object, OR from MULTIPLE source objects
  joined via an explicit `table.source_joins` declaration where every join is a plain
  equality-condition lookup (many-to-one from the driving/grain object's perspective -- a
  denormalizing dimension join, a header table rolled 1:many down to a fact's own grain, a
  dimension surrogate-key lookup), with no aggregation and no fan-out risk. Its tests are the kind
  `data-quality`/Declarative Pipeline expectations already express (nullability, uniqueness,
  range, referential). This is the common case for medallion silver->gold reshaping, and -- as of
  the multi-source join support added to `build_transform_spec.py` -- covers most "denormalize a
  snowflaked dimension" and "roll header attributes down to line-item grain" fact/dimension shapes
  too. See "Worked example: denormalizing lookup join vs. genuine multi-source complexity" below.
- `complex_procedural`: the target needs a join that ISN'T a plain equality lookup (a range/
  `BETWEEN` condition, a one-to-many join on the "wrong" side that would duplicate driving rows, a
  join needing aggregation to resolve), external API calls mid-pipeline, row-level branching that
  isn't expressible as a CASE expression, iterative/stateful logic, or a business rule that
  genuinely needs imperative code to express correctly. If `build_transform_spec.py` refuses
  (multi-source columns with no `source_joins` declaration, or a join that can't be expressed as
  the structured equality-only shape `source_joins` supports at all), that alone is enough to
  classify this as `complex_procedural` -- don't try to force a spec by picking one source and
  dropping the rest, and don't try to smuggle a non-equality condition into `source_joins` (the
  schema has no field for one; that absence is deliberate, not an oversight).

#### Worked example: denormalizing lookup join vs. genuine multi-source complexity

**Still `simple_declarative`** -- `dim_product` denormalizes `product` (driving), `product_category`
(joined twice: once directly on `CategoryID`, once more via `ParentProductCategoryID` for a
1-level parent category -- a self-join, disambiguated by giving each occurrence its own
`source_joins.joins[].alias`), and `product_model`. Every join is `LEFT JOIN ... ON <fk> = <pk>`,
many-to-one from `product`'s perspective -- exactly one `product_category`/`product_model` row can
ever match a given `product` row, so there is no fan-out risk and nothing to aggregate.
`source_is_managed_connector` is false (already-in-the-lakehouse silver), `target_layer` is gold,
`transform_complexity` is `simple_declarative` -> `declarative_pipeline`, same as any other
gold-layer reshape.

**Still `simple_declarative`, a different shape** -- `fact_sales_order_line`'s grain is
`(SalesOrderID, SalesOrderDetailID)` (the line-item table, driving), but `customer_key`,
`bill_to_address_key`, and the degenerate `sales_order_number` live one level up on the
order-header table, joined `1:many` down to the line grain on `SalesOrderID` (many line rows match
one header row -- many-to-one from the LINE's perspective, the direction that matters for fan-out
risk, not the header's). A third join resolves `order_date_key` against `dim_date.calendar_date`
using `CAST(header.OrderDate AS DATE)` as the join key (a `source_joins.joins[].on[].left_expression`,
not a bare column) -- still a plain equality condition once the cast is applied, still
many-to-one. Still `simple_declarative`.

**Now `complex_procedural`** -- the same `fact_sales_order_line`, but a hypothetical
`current_promotion_key` needs to look up `dim_promotion` on `promo.start_date <= header.OrderDate
AND promo.end_date >= header.OrderDate` (a range condition, not an equality) -- `source_joins` has
no field for this, `build_transform_spec.py` cannot render it, and forcing an approximate equality
join (e.g. matching only `start_date`) would silently produce wrong promotion attribution on any
order that doesn't fall exactly on a promotion's start date. Also `complex_procedural`: a
`fact_order_summary` that aggregates `sales_order_line` up to `SalesOrderID` grain before joining
to header (a GROUP BY has no home in `source_joins`, which only expresses row-preserving lookups)
-- correctly routes to `pyspark_notebook`, hand-authored, with the per-source column mappings
`build_transform_spec.py` can still surface (rerun it per source object) as a reference.

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
