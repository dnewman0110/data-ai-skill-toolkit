# Kimball concepts as this toolkit's schema expresses them

`contracts/model-spec.schema.json` encodes a specific, opinionated subset of Kimball dimensional
modeling. This file is the vocabulary, not a general dimensional-modeling textbook.

## Grain discipline

`facts[].grain.statement` is a plain-language sentence: "one row per X." Every measure on the fact
must be well-defined and correctly aggregable at exactly that grain --
`validated_against_measures: true` is a claim you only make after actually checking each measure,
not a default. If a measure ISN'T well-defined at the proposed grain (e.g. a "customer lifetime
value" measure doesn't belong on an order-line-grain fact), either the grain is wrong for that
measure or the measure belongs on a different fact -- don't force it in with a caveat.

## Measure additivity

Three categories, and the distinction is genuinely semantic, not something a SQL type reveals:

- **`additive`**: sums correctly across every dimension. `order_total_usd` on an order-line fact --
  sum across order lines, across customers, across time, all valid.
- **`semi_additive`**: sums correctly across SOME dimensions but not others. A classic example is
  an account balance snapshot -- sums correctly across accounts (total balance across all
  accounts on a given day) but NOT across time (summing a balance across 30 days of snapshots
  doesn't produce a meaningful "total"; you'd average or take the latest instead).
- **`non_additive`**: doesn't sum meaningfully across any dimension. A unit price, a ratio, a
  percentage -- combine these with a weighted average or a recomputation, never a SUM.

Ground the classification in what `validate_grain_against_measures.py`'s findings report (the
measure's declared type) plus your own judgment about what the number MEANS -- a `decimal` type
doesn't imply additive, and a value stored as `TEXT` (like this toolkit's own fixture's
`silver.orders.total_amt`) needs an explicit cast recorded in
`source_to_target_mappings[].transformation` before it can be treated as any numeric category at
all.

**`transformation` must be a bare, executable SQL expression -- no trailing `--` commentary
inside it.** `data-pipeline` (via `data-discovery`'s resolution mode) renders this string verbatim
into generated PySpark/SQL; a comment embedded in the expression risks being parsed as part of the
SQL by whatever consumes it, not treated as documentation. Use the sentinel `"direct"` for a plain
rename (no transformation needed), a bare expression like `"CAST(total_amt AS DECIMAL(18,2))"` or
`"DATEDIFF(check_out, check_in)"` for a real one, and put any rationale for *why* in this fact's
`assumptions[]` entry instead, same as any other design decision worth explaining.

## Multi-source facts and dimensions

Design the correct grain and mappings first, from the real silver objects the business need
actually requires -- never compromise the design because a single source object would be simpler
to specify. `data-pipeline` renders a real multi-table join for any fact or dimension whose
attributes genuinely come from more than one silver object, as long as every join is a plain
equality-condition lookup (many-to-one from the fact's/dimension's own grain object's
perspective) -- exactly the two shapes below. Declare `facts[].source_joins` or
`dimensions[].source_joins` (same structured shape in both) to carry the join through
`data-discovery`'s resolution mode into `data-contract.json` unchanged; `source_to_target_mappings[]`
/ `attributes[].source_mapping` then set `join_alias` to say which declared object each mapping
actually comes from.

- **A snowflaked/denormalized dimension**: a product dimension sourcing `product` (the grain
  object), `product_category` (joined on a foreign key), and `product_model` (joined on another) --
  the classic "flatten a snowflake into one wide dimension" case. If the same object needs joining
  more than once (a category self-joined via `ParentProductCategoryID` to also pull in a 1-level
  parent category name), give each occurrence its own `alias` in `source_joins.joins[]` -- the
  object repeats, the alias never does.
- **A fact whose attributes live at more than one grain**: an order-line fact whose own grain is
  `(SalesOrderID, SalesOrderDetailID)`, but several attributes (a customer key, a degenerate order
  number) live one level up on the order-header table, `1:many` rolled down to the line grain by
  joining on `SalesOrderID`. A further dimension surrogate-key lookup (resolving `order_date_key`
  by matching a cast source date against `dim_date.calendar_date`) is the same shape again --
  still a plain equality condition once the cast (`source_joins.joins[].on[].left_expression`,
  e.g. `"CAST(header.OrderDate AS DATE)"` -- same free-text-SQL, no-trailing-comment rule as
  `transformation` above) is applied.

**What still doesn't belong in `source_joins`, by design**: a join that isn't a plain equality
condition (a range/`BETWEEN` predicate), a join that would fan out the grain object's rows
(one-to-many in the wrong direction), or anything needing aggregation to resolve. If a design
genuinely needs one of these, that's usually a sign the grain or the join direction itself needs
re-thinking, not something to route around -- `data-pipeline` correctly refuses to render it and
routes to hand-authored `pyspark_notebook` instead (`transform_complexity: complex_procedural`,
see `skills/data-pipeline/references/decision-rubric.md`'s worked example).

## Degenerate dimensions

An operational identifier that lives ON the fact table itself rather than in its own dimension
table, because it has no attributes worth modeling separately -- an order number, an invoice
number. `facts[].degenerate_dimensions[]` records these as `{column, source_system_identifier}`,
not as entries in `dimensions[]`.

## Conformed, local, junk, and bridge dimensions

- **`conformed`**: shared across multiple facts/stars, built once. `conformance_group` is the
  identifier every fact/star referencing it uses, so it's reused rather than redefined per star --
  see `references/conformed-dimensions.md` for how candidates are discovered.
- **`local`**: specific to one fact/star, not intended for reuse.
- **`junk`**: a grab-bag of low-cardinality flags/indicators that don't deserve their own
  dimension each, combined into one to keep the fact table's foreign key count sane.
- **`bridge`**: resolves a multivalued relationship between a fact and a dimension (e.g. an order
  that can have multiple sales reps with different commission splits). `bridge_definition` records
  `resolves_multivalued_relationship_between` and, if applicable, a `weighting_factor_column` for
  splitting an additive measure proportionally across the bridge.
