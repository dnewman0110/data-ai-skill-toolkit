# Silver-layer verification: what's checked, and why it's not a quality check

`scripts/verify_silver_layer.py` gates every fact/dimension this skill proposes. Understanding
what it does and doesn't check matters, because it's easy to conflate "curated" with "clean" --
they're different properties, and this toolkit deliberately checks only the first one here.

## The five signals

1. **`primary_key_declared`** -- a PK constraint is actually declared on the object.
2. **`primary_key_profiled_unique`** -- the declared PK is ACTUALLY unique when profiled, not just
   declared. Unity Catalog primary/foreign key constraints are informational, not enforced by the
   engine -- a declared PK that isn't really unique is a real gap this signal exists to catch, not
   a redundant check.
3. **`table_comment_present`** -- someone documented what this table is.
4. **`no_raw_ingestion_artifact_columns`** -- no column named like a raw-ingestion artifact
   (`_rescued_data`, `_ingest_*`, `_load_*`, `_raw_*`, `_file_*`, `_batch_*`).
5. **`business_meaningful_naming_ratio >= 0.8`** -- at least 80% of columns are `snake_case`
   (business-conformed naming), not `PascalCase`/abbreviated source-system field codes.

`verified` requires all five. `layer_detected` is `bronze_or_raw` when no PK is declared AND
raw-ingestion columns are present, `silver_curated` when verified, `silver_uncurated` when a PK is
declared but something else fails (e.g. curated shape, missing documentation), `unknown`
otherwise. An object already in the `gold` schema short-circuits to `layer_detected: gold`,
`verified: true` without running the five checks -- gold is by definition at least as curated as
what this skill would itself produce.

## Why this checks structure, not data quality

A table can fail every check `data-quality` would run against it -- null rates, orphaned foreign
keys, duplicated natural keys -- and still pass THIS check, because those are quality problems a
genuinely curated layer can still have. The toolkit's own fixture lakehouse makes this concrete:
`silver.orders` has a broken FK, a nullable column that shouldn't be, and a source/target type
mismatch, and `silver.customers` has a duplicated natural key -- and both objects still verify
here, because they have declared PKs that profile as genuinely unique, table/column comments,
no raw-ingestion columns, and business-meaningful naming. That's the right outcome: those are real
problems, but they're `data-quality`'s and `data-discovery`'s problems to catch, not a reason to
block dimensional design. Conversely `bronze.raw_orders` could have perfectly internally-consistent
values and still correctly fail this check, because it has no declared PK, carries
`_rescued_data`/`_ingest_file_name`/`_ingest_ts`, and uses `OrderID`/`CustID`/`TotalAmt`-style
naming -- structurally, nobody has curated it yet, regardless of what its values look like.

## The override path

Sometimes a team genuinely needs to model against an object mid-migration, before its curation
catches up -- e.g. comments haven't been backfilled yet but everything else about the layer is
solid. `build_model_findings.py --force` skips the halt and lets the rest of the workflow proceed.
This is NEVER the default and NEVER inferred from an ambiguous request -- it requires an explicit,
in-conversation human instruction to proceed anyway, and the resulting `model-spec.json` still
reports `silver_verification.verified: false` and the real `reason_if_not_verified` honestly (this
skill does not "launder" an override into a false `verified: true`). Record the override itself in
`assumptions[]`: what wasn't verified, who said to proceed anyway, and why -- so a reviewer reading
the artifact later sees exactly what was skipped and on whose authority, not just a clean-looking
model-spec with no trace of the shortcut.
