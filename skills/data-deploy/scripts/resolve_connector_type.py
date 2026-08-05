#!/usr/bin/env python3
"""
resolve_connector_type.py -- deterministic lookup from a NAMED source system to the Lakeflow
Connect connector type identifier and Unity Catalog connection option shape it needs, per
references/connector-type-mapping.md.

source_system is never parsed or guessed from the upstream data-contract.json's `source.object`
naming convention -- that convention is not consistent across source systems in this toolkit
(a Salesforce landing's source.object is "salesforce_source.crm.opportunity", a SQL-Server-sourced
one is the real database name, e.g. "AdventureWorksLT.SalesLT.Product" -- see
skills/data-discovery/references/sqlserver-profiling.md and DECISIONS.md decision 58's
numeric_types audit for the same "don't string-match, it's inconsistent" lesson applied here). The
invoking agent names source_system explicitly (from engagement context, or by asking the human if
genuinely ambiguous) -- see SKILL.md step 2 and toolkit-conventions.md #5/#6: classification is
judgment, application (this lookup) is deterministic, and an unrecognized value halts rather than
guesses a plausible-looking connector_type.
"""
import argparse
import json
import sys

# Each entry: the Lakeflow Connect connector type identifier (Unity Catalog connection_type enum
# shape, e.g. "SQLSERVER"), the connection-level option keys a human fills in when creating the UC
# connection (never a literal secret value -- see auth_note), and a verification_note stating how
# confident this entry is against current public documentation. Extending this table for a source
# system not yet listed here is the intended way to add support for it -- see
# references/connector-type-mapping.md "Adding a new source system."
CONNECTOR_TYPES = {
    "sql_server": {
        "connector_type": "SQLSERVER",
        "connection_options": {
            "host": "<fill in: e.g. myserver.database.windows.net>",
            "port": "<fill in: e.g. 1433>",
            "database": "<fill in: source database name>",
            "trustServerCertificate": "<fill in: true|false>",
        },
        "auth_note": "user/password options resolve to a Databricks secret scope reference "
                     "(e.g. '{{secrets/scope/key}}'), never a literal credential -- see "
                     "toolkit-conventions.md #2. Azure AD auth is also supported by Lakeflow "
                     "Connect for Azure SQL Database; prefer it when available, same posture as "
                     "SqlServerAdapter's own azure_ad_default mode.",
        "verification_note": "High confidence -- SQL Server is a long-standing Lakehouse "
                              "Federation/Lakeflow Connect connector with a stable, documented "
                              "option shape.",
    },
    "salesforce": {
        "connector_type": "SALESFORCE",
        "connection_options": {},
        "auth_note": "OAuth-based -- the connection is authorized interactively via the "
                     "Databricks UI or `databricks connections create`'s OAuth flow, not static "
                     "config keys. No credential material belongs in connection_options at all.",
        "verification_note": "High confidence -- Salesforce is one of Lakeflow Connect's original "
                              "managed connectors.",
    },
    "servicenow": {
        "connector_type": "SERVICENOW",
        "connection_options": {
            "url": "<fill in: e.g. https://mycompany.service-now.com>",
        },
        "auth_note": "OAuth-based, authorized interactively via the Databricks UI/CLI, same "
                     "posture as salesforce above -- no credential material in connection_options.",
        "verification_note": "Documented, not independently verified against a live workspace by "
                              "this toolkit -- verify connection_options against your Lakeflow "
                              "Connect release before an engagement, same discipline as "
                              "DECISIONS.md decision 58.",
    },
    "workday": {
        "connector_type": "WORKDAY",
        "connection_options": {
            "host": "<fill in: e.g. mycompany.workday.com>",
            "report_parameters": "<fill in: RaaS report entity/parameters this connection reads>",
        },
        "auth_note": "OAuth-based, authorized interactively via the Databricks UI/CLI -- no "
                     "credential material in connection_options.",
        "verification_note": "Lower confidence than sql_server/salesforce -- Workday ingestion is "
                              "RaaS-report-shaped, which varies more by engagement than a "
                              "database or CRM connector. Verify connection_options against your "
                              "Lakeflow Connect release and the specific Workday report before an "
                              "engagement.",
    },
    "sharepoint": {
        "connector_type": "SHAREPOINT",
        "connection_options": {
            "site_url": "<fill in: e.g. https://mycompany.sharepoint.com/sites/mysite>",
        },
        "auth_note": "OAuth-based, authorized interactively via the Databricks UI/CLI -- no "
                     "credential material in connection_options.",
        "verification_note": "Documented, not independently verified against a live workspace by "
                              "this toolkit -- verify connection_options against your Lakeflow "
                              "Connect release before an engagement.",
    },
}


def resolve_connector_type(source_system: str) -> dict:
    key = source_system.strip().lower().replace(" ", "_").replace("-", "_")
    if key not in CONNECTOR_TYPES:
        supported = ", ".join(sorted(CONNECTOR_TYPES))
        raise ValueError(
            f"Unrecognized source_system '{source_system}' -- supported: {supported}. "
            "Not guessing a connector_type for an unlisted system; see "
            "references/connector-type-mapping.md 'Adding a new source system' to extend this "
            "table, or name a supported source_system explicitly."
        )
    entry = CONNECTOR_TYPES[key]
    return {"source_system": key, **entry}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-system", required=True)
    args = parser.parse_args()

    try:
        result = resolve_connector_type(args.source_system)
    except ValueError as e:
        print(json.dumps({"halted": True, "reason": str(e)}, indent=2))
        sys.exit(1)

    print(json.dumps({"halted": False, **result}, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
