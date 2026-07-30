# Staged comparison

`skills/data-validation/scripts/compare_staged.py` runs exactly four stages, in order, digging
deeper only when a shallower stage found (or couldn't rule out) a discrepancy.

## Stage 1: `row_count`

`adapter.row_count(schema, table, exact=True)` on both sides -- a single `COUNT(*)`, pushed down,
cheap regardless of table size. **Always executed. Never itself a stopping point** -- matching row
counts do not prove matching content (two tables can have the same count and different rows), so a
count match alone is not treated as "done."

## Stage 2: `hash_aggregate`

The real first stopping point. Both sides are fetched (columns in the compare set, ordered by the
declared/candidate key, capped at `content_check_row_cap`), each row is normalized (see
`references/normalization-and-type-coercion.md`) and hashed, and the per-row hashes are combined
into one order-independent aggregate per side via XOR (so fetch order, which is not guaranteed to
match between two independent queries -- possibly against two different connections entirely --
never causes a false mismatch). **If the aggregates match, the comparison stops here.**
`column_aggregate` and `row_level_diff` are recorded as `executed: false` -- there's nothing more
to find, and no reason to pay for finding it.

## Stage 3: `column_aggregate`

Only runs if stage 2 found a mismatch. Computed from the SAME fetched-and-normalized rows stage 2
already pulled -- no second fetch. Per compare-column: row count, null count, and (for
numeric-treated columns) sum, compared between sides. This is triage, not diagnosis: it tells you
which columns *might* be involved before you look at individual rows. **Never itself a stopping
point** -- if stage 2 found a mismatch, something concrete differs, and only stage 4 identifies
which rows.

## Stage 4: `row_level_diff`

Also from the same fetched data. Rows are keyed by the declared/candidate key on each side; the
comparison computes: keys present in source but not target (`missing_from_target`), keys present in
target but not source (`extra_in_target`), and keys present on both sides whose normalized-row hash
differs (`changed`, with `columns_affected` listing exactly which columns disagree). **Always the
last stage** -- always `stopped_here: true` when it runs, since there's nowhere deeper to go.
Discrepancies are reported up to `row_level_diff_row_cap` (missing/extra keys first, then changed
keys, until the cap is spent) -- capping what gets full detail in the report, not what gets
compared; the comparison itself already covers every fetched row.

## Known scaling limit (read this before trusting a huge-table run)

Stages 2-4 fetch every row on both sides, up to `content_check_row_cap` (default 100,000 -- see
`toolkit.yaml`'s `validation` block). Above that cap, this implementation reports `row_count` only
and marks the deeper stages `executed: false` with a stated `skipped_reason`, rather than silently
sampling and calling the comparison complete. This is a real, honestly-disclosed limitation: a
production-scale implementation comparing multi-million-row tables would need to push
hash/aggregate computation down via SQL on each side (e.g. a `HASH()`/`sha2()`-based per-row
fingerprint computed in-database, aggregated with a commutative combiner, entirely server-side)
instead of fetching rows to Python. That's a meaningfully larger build -- portable hashing across
SQLite, Spark SQL, and whatever a cross-platform target speaks is not a small addition -- and isn't
implemented here. If you hit this limit on a real engagement, that's the signal to either partition
the comparison (e.g. by date range, run validation per-partition) so each run stays under the cap,
or invest in the SQL-pushdown version for that specific platform pair.

## Cross-platform vs. same-platform

`compare()` takes independent `source_adapter`/`target_adapter` instances -- they don't have to be
the same connection, or even the same platform. This toolkit ships two adapters
(`scripts/lakehouse_adapter.py`): `SQLiteFixtureAdapter` (evals/local) and `DatabricksAdapter`
(production, Databricks-to-Databricks). Genuinely cross-platform validation (e.g. a SQL Server
source against a Databricks target) is designed for -- the interface doesn't assume same-platform --
but isn't implemented: it would need a new `LakehouseAdapter` subclass for the other platform, at
which point `compare_staged.py`'s logic works unmodified. When `source.platform != target.platform`
in the final report, `type_coercion_map[]` is required (non-null) and should state, per compared
column, how each side's type was coerced to a common comparable type -- normally the same
`infer_column_treatment` numeric/timestamp decision the normalizer already makes, made explicit for
the report rather than left implicit.
