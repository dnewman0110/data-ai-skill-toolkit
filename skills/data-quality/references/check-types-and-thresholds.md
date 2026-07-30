# Check types and thresholds

`skills/data-quality/scripts/run_checks.py` implements all seven check types from
`contracts/quality-report.schema.json`'s `checks[].type` enum. One rule applies to every type:
**a check with no configured threshold is `not_evaluated`, never a silent always-pass.** A quality
report that shows "passed" for a check nobody actually set a bar for is worse than one that
honestly says "not evaluated: no threshold configured."

## Definition format

A check definition (in a hand-authored checks file, or produced by
`derive_checks_from_contract.py`):

```json
{
  "check_id": "orders.ship_region.null_rate",
  "type": "null_rate",
  "column": "ship_region",
  "params": { "max_null_rate": 0.03 },
  "severity": "warning"
}
```

`check_id` is optional on input (auto-generated from table/column/type if omitted) but always
present in output. `column` is omitted for `row_count` and `custom_sql`.

## Status computation (same rule, every type)

1. Can't execute at all (missing column, malformed/missing params, a referenced object that
   doesn't exist, a `custom_sql` execution error) -> **`not_evaluated`**, with
   `reason_not_evaluated` naming exactly what went wrong.
2. Executes, meets threshold -> **`passed`**.
3. Executes, violates threshold -> **`failed`** if `severity: blocking`, **`warned`** if
   `severity: warning`. `severity` is a property of the check (how much it matters, configured by
   whoever wrote it); `status` is the measured outcome. Keeping them separate means a
   warning-severity check that fails still shows up distinctly as `warned`, not disguised as
   either a pass or a blocking failure.

## The seven types

| Type | Required params | Measured value | Violates when |
|---|---|---|---|
| `row_count` | `min_rows` and/or `max_rows` | row count | outside `[min_rows, max_rows]` |
| `null_rate` | `max_null_rate` | observed null rate | `null_rate > max_null_rate` |
| `uniqueness` | `columns` (list -- single or composite) | `{duplicate_count, rows_checked}` | any duplicates (NULLs excluded per standard UNIQUE semantics -- see `scripts/lakehouse_adapter.py`'s `check_uniqueness`) |
| `value_range` | `min` and/or `max` | `{observed_min, observed_max}` | observed extreme falls outside the configured bound |
| `referential` | `ref_object`, `ref_column`; `max_orphan_rate` (default 0) | `{orphan_count, orphan_rate}` | `orphan_rate > max_orphan_rate` |
| `freshness` | `max_staleness_days` | days since the column's max value | `staleness_days > max_staleness_days` |
| `custom_sql` | `sql` (a single `SELECT`), `expected`, `comparison` (`equals`/`max`/`min`, default `equals`) | the scalar the query returns | doesn't satisfy the comparison against `expected` |

## `custom_sql`: the one check type that runs caller-supplied SQL

`sql` must be a single `SELECT` statement -- `scripts/lakehouse_adapter.py`'s
`assert_read_only_select` rejects anything containing `INSERT`/`UPDATE`/`DELETE`/`DROP`/`ALTER`/
`CREATE`/`TRUNCATE`/`GRANT`/`REVOKE`/`MERGE`/`EXEC`/`EXECUTE`/`CALL`, or more than one statement.
**This is defense-in-depth, not a substitute for connecting with a genuinely read-only
credential** -- see `references/toolkit-conventions.md` #1. Use `custom_sql` for a check the other
six types can't express (a cross-column business rule, an aggregate condition); reach for one of
the other six first when they fit, since their intent is self-documenting from the check
definition alone in a way an arbitrary SQL string isn't.

## Freshness caveat

`freshness` compares the column's observed max value (parsed as a date/timestamp) against the
CURRENT time, unlike `data-discovery`'s test proposal, which deliberately does NOT auto-propose
freshness tests because it has no principled way to derive a staleness threshold from profiling
alone (see `skills/data-discovery/references/grain-and-tests.md`). Here, in `data-quality`,
`max_staleness_days` is a genuine input a human configures based on the business's actual freshness
requirement (e.g. "orders should never be more than 1 day stale") -- it is NOT derived from
profiling, and that's the right division of labor: discovery measures and proposes what it can
support with evidence; quality runs a freshness bar a human actually set.
