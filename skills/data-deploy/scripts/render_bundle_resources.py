#!/usr/bin/env python3
"""
render_bundle_resources.py -- renders the two Databricks Asset Bundle resource files
(uc_connection.yml, ingestion_pipeline.yml) for one approved target, using the templates under
skills/data-deploy/templates/ and the connector-type shape resolve_connector_type.py already
resolved. Parameterized entirely by the target's own fields (catalog, schema, table, merge_keys,
source object) -- nothing here is specific to any one source system or client engagement; the
templates are generic across whatever connector_type was resolved.

Only merge_keys drives an optional table_configuration.primary_keys block -- Lakeflow Connect's
managed connectors (Salesforce, ServiceNow, Workday, SharePoint) generally determine primary keys
for standard objects internally and this block is not needed for them, but a database source (SQL
Server) or a custom object may need it stated explicitly. Rendered when merge_keys is non-empty
regardless of connector_type, since whether a given connector honors or ignores an explicit
primary_keys list is a per-connector-type detail this toolkit does not model -- see
references/asset-bundle-resources.md.
"""
import string
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _render_connection_options_yaml(options: dict) -> str:
    if not options:
        return "    {}"
    return "\n".join(f'    {key}: "{value}"' for key, value in options.items())


def _render_primary_keys_block(merge_keys: list) -> str:
    if not merge_keys:
        return ""
    keys_yaml = ", ".join(f'"{k}"' for k in merge_keys)
    return (
        "\n              table_configuration:\n"
        f"                primary_keys: [{keys_yaml}]"
    )


def render_bundle_resources(table_name: str, target_catalog: str, target_schema: str,
                             source_schema: str, source_table: str, merge_keys: list,
                             connector_info: dict, connection_name: str, output_dir: Path) -> dict:
    target_dir = output_dir / "generated" / table_name
    target_dir.mkdir(parents=True, exist_ok=True)

    connection_tmpl = string.Template((TEMPLATES_DIR / "uc_connection.yml.tmpl").read_text())
    connection_yaml = connection_tmpl.substitute(
        table_name=table_name,
        connection_name=connection_name,
        connector_type=connector_info["connector_type"],
        auth_note=connector_info["auth_note"],
        connection_options_yaml=_render_connection_options_yaml(connector_info["connection_options"]),
    )
    connection_path = target_dir / "uc_connection.yml"
    connection_path.write_text(connection_yaml)

    pipeline_tmpl = string.Template((TEMPLATES_DIR / "ingestion_pipeline.yml.tmpl").read_text())
    pipeline_yaml = pipeline_tmpl.substitute(
        table_name=table_name,
        target_catalog=target_catalog,
        target_schema=target_schema,
        connection_name=connection_name,
        source_schema=source_schema,
        source_table=source_table,
        primary_keys_block=_render_primary_keys_block(merge_keys),
    )
    pipeline_path = target_dir / "ingestion_pipeline.yml"
    pipeline_path.write_text(pipeline_yaml)

    return {
        "generated_files": [
            {"path": str(connection_path.relative_to(output_dir)), "purpose": "uc_connection_definition"},
            {"path": str(pipeline_path.relative_to(output_dir)), "purpose": "ingestion_pipeline_resource"},
        ]
    }
