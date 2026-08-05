# Connector-type mapping

`scripts/resolve_connector_type.py` maps a NAMED source system to the Lakeflow Connect connector
type identifier and Unity Catalog connection option shape it needs. This doc explains why the
mapping key is a name the agent supplies rather than something parsed out of the upstream
`data-contract.json`, and how to extend the table.

## Why `source_system` is named explicitly, not parsed from `source.object`

It would be convenient if a target's source system could be read straight off its
`data-contract.json` column mappings' `source.object` field. It can't be, reliably, across every
source system this toolkit already supports:

- A Salesforce bronze landing's `source.object` looks like `salesforce_source.crm.opportunity` --
  the first segment is a naming-convention placeholder, not a real Unity Catalog catalog, and it
  happens to end in `_source`.
- A SQL-Server-sourced object's `source.object` looks like `AdventureWorksLT.SalesLT.Product` --
  the first segment is the REAL source database name (per
  `skills/data-discovery/references/sqlserver-profiling.md`'s documented `<database>.<schema>.<table>`
  convention), with no marker identifying it as SQL Server at all.

String-matching on `source.object`'s shape would work for Salesforce and silently misclassify (or
just fail to classify) SQL Server, and there is no guarantee a future source system's convention
looks like either of these. Guessing a source system from an inconsistent naming convention is
exactly the kind of silent inference `references/toolkit-conventions.md` #6 says not to do --
guessing wrong here doesn't just mislabel a finding, it renders a UC connection resource with the
wrong `connection_type` entirely, which does not fail loudly at YAML-render time.

Per `toolkit-conventions.md` #5 ("classification is judgment, application is deterministic"), the
invoking agent names `source_system` explicitly -- from engagement context it already has (a human
said "here's the Salesforce contract," or the data-contract's `assumptions[]`/table names make it
unambiguous), or by asking the human if it's genuinely unclear. `resolve_connector_type.py` then
applies that name deterministically, and refuses (never guesses a plausible-looking fallback) if
the name isn't in the table below.

## Supported source systems

| `source_system` | `connector_type` | Auth shape | Confidence |
|---|---|---|---|
| `sql_server` | `SQLSERVER` | Username/password (via a Databricks secret scope reference) or Azure AD | High -- long-standing, stable connector |
| `salesforce` | `SALESFORCE` | OAuth, authorized interactively via UI/CLI | High -- original Lakeflow Connect managed connector |
| `servicenow` | `SERVICENOW` | OAuth, authorized interactively via UI/CLI | Documented, not independently verified against a live workspace |
| `workday` | `WORKDAY` | OAuth, authorized interactively via UI/CLI | Lower -- RaaS-report shape varies more by engagement than a database/CRM connector |
| `sharepoint` | `SHAREPOINT` | OAuth, authorized interactively via UI/CLI | Documented, not independently verified against a live workspace |

Every entry's exact `connection_options` keys are documented, not verified against a live
Databricks workspace by this toolkit's own evals (the same limitation `SqlServerAdapter` and
`DatabricksConnectAdapter` have always had -- see `DECISIONS.md` decisions 54 and 58). **Run
`databricks bundle validate` against the generated resources, and confirm the connector's actual
option keys in the Databricks documentation for your workspace's release, before an engagement.**

## Adding a new source system

Add an entry to `CONNECTOR_TYPES` in `scripts/resolve_connector_type.py`: the `connector_type`
identifier, the `connection_options` keys a human fills in (never a literal secret -- see
`toolkit-conventions.md` #2), an `auth_note`, and a `verification_note` stating how confident the
entry is. Add a row to the table above. No other file needs to change -- `render_bundle_resources.py`
and the templates are already generic over whatever `resolve_connector_type.py` returns.
