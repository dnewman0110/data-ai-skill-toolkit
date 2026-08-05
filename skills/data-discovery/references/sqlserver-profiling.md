# Profiling a SQL Server source before ingestion

`data-discovery` can profile a SQL Server database directly, before anything from it has ever
landed in the lakehouse -- greenfield mode, `--backend sqlserver`. The point is to surface real
data-quality problems (a broken key, a duplicated natural key, a column that's TEXT but should be
numeric, a null rate that looks accidental) *before* anyone spends engineering time designing or
building the ingestion pipeline for it, using the exact same deterministic profiling
(`profile_object.py`, `propose_tests.py`) this skill already runs against Databricks sources --
`SqlServerAdapter` (`scripts/lakehouse_adapter.py`) is the only new code; nothing about how
findings get produced or interpreted changes.

## Setting up `toolkit.yaml`

```yaml
external_sources:
  sqlserver:
    enabled: true
    host: "acmecrm.database.windows.net"
    port: 1433
    driver: "ODBC Driver 18 for SQL Server"
    auth_mode: azure_ad_default
    username_env_var: null
    password_env_var: null
```

Three `auth_mode` values, all ambient -- **no credential value ever lives in `toolkit.yaml`, is
typed into a conversation, or is echoed by this skill** (`references/toolkit-conventions.md` #2):

- **`azure_ad_default`** (recommended, and the default): the SQL Server is plausibly Azure SQL
  Database or Managed Instance, since this toolkit's default target environment is
  Databricks-on-Azure. `SqlServerAdapter` fetches a token from whatever's already logged in --
  `az login`, a managed identity, an env-based service principal (`azure-identity`'s
  `DefaultAzureCredential`, which tries those in order) -- and passes it to the SQL Server driver
  as an access token. No username or password concept exists in this mode at all. This is the
  direct parallel to `environment.backend: databricks_connect` reusing an already-authenticated
  Databricks Connect session; set it up once outside this toolkit (log into Azure in the host
  environment) and every run just works.
- **`sql_auth_env`**: for a SQL Server that authenticates by username/password rather than Azure
  AD. Set `username_env_var`/`password_env_var` to the *names* of environment variables --
  `"SQLSERVER_USERNAME"`, `"SQLSERVER_PASSWORD"`, whatever you call them -- and set those
  variables in your own shell before running Claude Code. `SqlServerAdapter` reads
  `os.environ[...]` for those names at connection time; if either is unset, the run halts with a
  message naming exactly which variable is missing, same posture as any other missing-config halt
  in this toolkit. The agent running this skill never resolves, prints, or reasons about the
  actual value -- only the name ever appears in a tool call.
- **`windows_integrated`**: for an on-prem SQL Server where the host machine's own domain identity
  already has access. Trusted connection, no credential material of any kind.

`enabled: false` (the default in `toolkit.example.yaml`) means this block is ignored; nothing about
existing `sqlite_fixture`/`databricks_connect` engagements changes.

## What's different from profiling a Databricks source

SQL Server's SQL dialect (T-SQL) and system metadata differ enough from both SQLite and Spark SQL
that `SqlServerAdapter` translates every query, not just table addressing:

- `TOP (n)` instead of `LIMIT` for sampling.
- Table/column listing and nullability from `INFORMATION_SCHEMA.TABLES`/`.COLUMNS` (portable
  across SQL Server versions, unlike Unity Catalog's per-catalog-qualified equivalent).
  Primary/foreign keys from `INFORMATION_SCHEMA.KEY_COLUMN_USAGE`/`.TABLE_CONSTRAINTS`/
  `.REFERENTIAL_CONSTRAINTS` -- SQL Server's documented, version-stable way to map an FK
  constraint to the actual column it references.
- Column and table comments come from `sys.extended_properties` (`MS_Description`) -- SQL
  Server's equivalent of a Unity Catalog column/table comment, but stored as an extended property
  rather than a first-class `INFORMATION_SCHEMA` field, so it's a separate joined query.
  `full_data_type` fidelity (`decimal(18,2)`, `varchar(50)`) comes from
  `INFORMATION_SCHEMA.COLUMNS`' precision/scale/length fields, same spirit as Databricks'
  `full_data_type` -- but if those come back null for an unusual type, the bare type name is used
  rather than guessing at a shape.
- Row-count/size estimates (the cost gate's `row_count(exact=False)`/`estimate_bytes`) use
  `sys.partitions`/`sys.dm_db_partition_stats` -- the standard cheap, no-full-scan tricks SQL
  Server's own `sp_spaceused` uses internally, not a `COUNT(*)`/full read.
- The FK-orphan check uses `NOT EXISTS`, not `NOT IN` -- T-SQL's well-known trap where `NOT IN`
  silently evaluates to `UNKNOWN` (reporting zero orphans, wrongly) the moment the referenced-value
  subquery contains even one `NULL`.

None of this changes what a `findings[]` entry or `proposed_tests` entry looks like -- the output
shape downstream of the adapter is identical to a Databricks-sourced run.

## The resulting `data-contract.json`

`source.object` has no fixed format in `data-contract.schema.json` (confirmed by this toolkit's
own `evals/fixtures/salesforce-bronze-contract.json`, which already references
`salesforce_source.crm.opportunity` -- not a real Unity Catalog path). For a SQL Server source, use
`<database>.<schema>.<table>`; `target_catalog`/`target_schema` propose where it would land in
bronze. A worked example, profiling `acmecrm.dbo.customers` ahead of building its ingestion:

```json
{
  "name": "bronze_sqlserver_customers",
  "target_catalog": "acme_retail_dev",
  "target_schema": "bronze",
  "columns": [
    {
      "name": "customer_id", "type": "int", "nullable": false, "pii_tag": null,
      "source": { "object": "acmecrm.dbo.customers", "column": "customer_id", "mapping_type": "explicit_alias" }
    },
    {
      "name": "email", "type": "string", "nullable": true, "pii_tag": "email",
      "source": { "object": "acmecrm.dbo.customers", "column": "email", "mapping_type": "explicit_alias" }
    }
  ]
}
```

Columns map `explicit_alias`, 1:1, no reshaping -- a landing table takes the source as-is, exactly
the same posture this toolkit already takes for any other managed-connector landing (see
`evals/fixtures/salesforce-bronze-contract.json`). If profiling surfaced a real problem (an orphan
FK, a duplicated natural key, a TEXT column that should be numeric), it still goes in
`assumptions[]` per the normal greenfield workflow -- that's the whole point of profiling before
ingestion: the human designing the pipeline sees it up front, in the contract, not after building
around a bad assumption.

## What happens next

`references/decision-rubric.md`'s `source_is_managed_connector` factor already names SQL Server
explicitly as a Lakeflow Connect managed-connector source -- a bronze-landing contract like the one
above classifies correctly under `data-pipeline`'s existing modality rubric with **no changes to
that skill**. Hand the validated contract to `data-pipeline` the same way as any other; it'll
recommend `lakeflow_connect` and generate the connector-config stub.

`data-quality` doesn't apply here -- its scope is an object already landed in the lakehouse, not a
pre-ingestion external source. Once the table actually lands, though, comparing the original SQL
Server data against what landed is exactly `data-validation`'s job (source vs. target) and would
reuse this same adapter for the "source" side -- a natural extension, not built as part of this
feature.

## Verification status

Like `DatabricksConnectAdapter`, `SqlServerAdapter` is implemented against documented T-SQL/
`INFORMATION_SCHEMA`/`sys.*` APIs and pyodbc's documented connection options, not verified against
a live SQL Server by this toolkit's own evals (no live database in CI). The deterministic coverage
that *does* exist (`skills/data-discovery/evals/run_assertions.py`) mocks the pyodbc connection to
check generated-SQL shape, not correctness against a real server. Verify against a real (sandbox)
SQL Server or Azure SQL Database before relying on this in an engagement -- this toolkit's own
history (`DECISIONS.md` decision 54) is a direct precedent for a doc-accurate adapter still having
real bugs a live run catches immediately, and decision 58 is exactly that happening to this
adapter: five real bugs (a missing derived-table alias, `MIN`/`MAX` on unsupported types, an
aggregate-over-subquery T-SQL rejection in `count_orphans`, a missing-isolation crash on a single
bad check, and a `numeric_types` gap producing a factually wrong finding) surfaced the first time
this adapter ran against a live Azure SQL Database, none of which the mocked-connection evals
caught since they check SQL shape, not a real server's actual acceptance/rejection of that SQL.
