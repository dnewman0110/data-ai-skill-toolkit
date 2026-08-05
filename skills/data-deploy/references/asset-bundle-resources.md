# Asset Bundle resources this skill generates

Two files per processed target, under `output_dir/generated/<table_name>/`:

## `uc_connection.yml`

Documents the Unity Catalog connection the target's ingestion pipeline depends on: `name`,
`connection_type` (from `scripts/resolve_connector_type.py`), and `options` (connector-specific
config keys, `<fill in: ...>` placeholders -- never a literal credential, per
`toolkit-conventions.md` #2).

**This is documentation of a connection to create, not a bundle-native resource this skill has
confirmed Databricks Asset Bundles support creating directly** (as of this template's writing,
Asset Bundles' first-class resource types are things like `pipelines`, `jobs`, and `schemas` --
whether a top-level `resources.connections` type exists in your Databricks CLI version is
something to check, not something this toolkit asserts). The declarative shape rendered here is
templated from the Terraform provider's `databricks_connection` resource schema, which is stable
and well-documented independent of Asset Bundle version. Create the connection once -- it is
normally shared across every pipeline sourced from the same system/credential, not created per
target table -- via either:

```
databricks connections create --json '{"name": "...", "connection_type": "...", "options": {...}}'
```

or the Terraform `databricks_connection` resource with the equivalent shape. **Verify this against
your workspace's actual Databricks CLI/Terraform provider version before deploying** -- the same
"verify against live before an engagement" discipline `DECISIONS.md` decision 58 establishes for
`SqlServerAdapter`.

## `ingestion_pipeline.yml`

A genuine Databricks Asset Bundle resource: `resources.pipelines.<table>_ingestion` with an
`ingestion_definition` referencing the connection above by `connection_name`, and one `objects[]`
entry mapping the resolved source object (`source_schema`/`source_table`, read from the source
pipeline-manifest target's `transform_spec.json` -- never guessed) to the destination
(`destination_catalog`/`destination_schema`/`destination_table`, from the pipeline-manifest
target's own `target_catalog`/`target_schema`/`table_name`). This is the file a human wires into
the target project's `databricks.yml` via an `include:` entry (e.g. `include: [resources/*.yml]`)
-- `data-deploy` never edits a client project's `databricks.yml` directly, since that file lives
outside `output_dir` and editing it would be exactly the kind of uncontrolled write
`toolkit-conventions.md` #1 rules out.

### `table_configuration.primary_keys`

Rendered only when the pipeline-manifest target's `merge_keys` is non-empty, regardless of
connector type. Whether a given Lakeflow Connect connector honors an explicit `primary_keys` list
or determines it internally for standard objects (most managed SaaS connectors do, for their own
well-known object types) is a per-connector, per-object detail this toolkit does not model --
render it when there's a merge key to render, and let `databricks bundle validate` (or the
connector's own behavior) be the actual authority on whether it's used.

## Multiple targets in one pipeline-manifest

A pipeline-manifest can list more than one target table, but its `deployment.target_named` names
exactly one. `data-deploy` renders these two files only for that one target; every other target
gets a `skipped: true` entry in `deployment-manifest.json` with a reason, never a silent omission
-- see `references/approval-gate.md` and `SKILL.md`.
