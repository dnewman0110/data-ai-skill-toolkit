# Lakeflow Connect modality

## When this is the right choice

Only when `source_is_managed_connector: true` AND `target_layer: bronze` -- the source is an
external system Lakeflow Connect has a managed connector for, and no transformation happens at
ingestion. This is narrower than it might sound: most targets in a typical engagement are
already-in-the-lakehouse reshaping (silver->gold), which is `declarative_pipeline` territory, not
this. Lakeflow Connect is specifically for the FIRST hop -- getting external data into bronze.

## Why this generates a config stub, not code

Lakeflow Connect ingestion is configured (through the Databricks UI, CLI, or Terraform provider),
not hand-coded as a notebook or pipeline definition -- there is no PySpark or SQL for this skill to
generate. `connector_config.yaml` names the pieces a human fills in (connector type, source
object, destination, merge keys, sync schedule) rather than being an executable artifact itself.
This is the one modality where "generated_files" in the manifest is a stub for a human to complete
via the platform's own configuration surface, not a finished code artifact.

## Why `idempotency_check.result` is `not_applicable`, not `match`/`mismatch`

Lakeflow Connect manages CDC and incremental sync state internally -- there is no merge/upsert
logic this toolkit generates or can locally test for this modality. Recording `not_applicable`
rather than fabricating a `match` result keeps the manifest honest about what was and wasn't
proven: idempotency here is the connector's own concern, verified by Databricks, not by this
toolkit's local mock-data proof (`references/idempotency-and-mock-data.md`).

## Deployment

Generating this config stub does not create, configure, or start any ingestion connector -- see
`toolkit-conventions.md` #1 and #7. A human takes the filled-in config to the Databricks UI,
CLI, or Terraform provider to actually stand up the connector.
