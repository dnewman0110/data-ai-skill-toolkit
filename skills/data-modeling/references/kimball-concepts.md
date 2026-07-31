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
