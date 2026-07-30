# Idempotency evidence and mock data: exactly what's proven, and what isn't

This toolkit's evals run with no live Databricks/Spark workspace (see
`scripts/lakehouse_adapter.py`'s own docstring on the same constraint for `DatabricksConnectAdapter`).
Rather than skip idempotency evidence for `data-pipeline` entirely, `validate_pipeline_locally.py`
proves what it CAN prove locally, and this file is explicit about the boundary so nobody mistakes
"validated" for "deployed and confirmed working."

## What `idempotency_check` proves

The transform_spec's merge-key logic and column mapping, rendered as a portable SQL
`INSERT ... ON CONFLICT DO UPDATE` (SQLite's upsert syntax, standing in for Delta `MERGE`/
`apply_changes`), applied TWICE against the same synthetic mock dataset in a scratch in-memory
SQLite database, produces an identical destination (same row count, same order-independent content
hash) both times. That's a real, reproducible proof that the LOGIC is idempotent against a
representative dataset shape.

## What it does NOT prove

- That the GENERATED Spark code (the actual `.py`/`.yaml` files) is syntactically or semantically
  correct Spark/Delta/DLT -- `generate_pipeline_code.py`'s Python outputs are `compile()`-checked
  for syntax only (see `skills/data-pipeline/evals/run_assertions.py`), never executed as Spark.
  There is no PySpark runtime in this toolkit's CI.
- That the pipeline behaves the same against REAL source data, which is messier, larger, and can
  have exactly the kind of source/target type mismatches the fixture lakehouse itself plants
  (`silver.orders.total_amt` is TEXT; `fct_orders.order_total_usd` is declared `decimal`) --
  mock data is shaped using the TARGET column's declared type, not the source's actual one (see
  `derive_mock_data.py`'s docstring), so it cannot surface a type-coercion bug the real source
  would hit.
- That the code runs at all on a real cluster, connects successfully, or has correct catalog/
  schema permissions.

`readiness_level: validated` means the idempotency proof above passed and every generated file
parsed/compiled cleanly -- it is a real, evidence-backed gate, just a narrower one than "this is
safe to deploy." The human review gate before deployment (`toolkit-conventions.md` #7 gate 3)
exists precisely because this evidence, while genuine, is not sufficient on its own.

## How mock data is derived

`derive_mock_data.py` reads ONLY a `data-contract.json` table's declared columns
(name/type/nullable), its `tests[]` (nullability rate, uniqueness key columns), and generates
synthetic rows deterministically (seeded, default 1337) -- key/id-shaped columns get a
row-index-based value guaranteeing uniqueness across the mock set, nullable columns get nulled at
the declared (or a light default 5%) rate, and everything else gets a type-appropriate synthetic
value. No client data is read at any point -- there is nothing in this script's inputs that could
leak client data even by accident, which is a stronger property than "redacted," and is worth
noting given `toolkit-conventions.md` #3's redaction rules exist specifically because OTHER skills
do read real sample rows.
