#!/usr/bin/env python3
"""
lakehouse_adapter.py -- backend-agnostic interface every skill's deterministic scripts use to
talk to "the lakehouse", so the same profiling/scan code runs against the local synthetic
fixture (offline, in evals/CI) and a real Databricks/Unity Catalog workspace (in production)
without a single skill script knowing which one it's talking to.

Three backends:
  - SQLiteFixtureAdapter: used for evals and local development against fixtures/. Requires
    nothing beyond the Python standard library (sqlite3) -- deliberately chosen so running a
    skill's evals never requires installing anything. Simulates Unity Catalog's catalog.schema.table
    addressing by ATTACHing one SQLite file per schema (bronze.db, silver.db, gold.db) to a single
    connection and addressing tables as "<schema>.<table>".
  - DatabricksConnectAdapter: used in production. Talks to Unity Catalog via Databricks Connect --
    whatever authenticated Spark session is already configured in the host environment (a
    databricks-connect profile, DATABRICKS_HOST/DATABRICKS_TOKEN, OAuth, etc. -- this class does
    not configure or manage that, it just grabs the ambient session), using native
    catalog.schema.table addressing and information_schema for metadata. Not exercised by this
    toolkit's own evals (no live workspace in CI) -- correctness here rests on matching the
    documented databricks-connect/information_schema APIs, not on a test run. Any skill built
    against LakehouseAdapter's interface should work against either backend unmodified.
  - SqlServerAdapter: used by data-discovery to profile a SQL Server source BEFORE it's ingested
    into the lakehouse at all (see skills/data-discovery/references/sqlserver-profiling.md) --
    `catalog` plays the same role as SQL Server's "database". Auth is ambient, same posture as
    DatabricksConnectAdapter: toolkit.yaml names connection shape and an auth_mode
    (azure_ad_default | sql_auth_env | windows_integrated), never a secret value itself -- see
    references/toolkit-conventions.md #2. Requires `pyodbc` (and, for azure_ad_default,
    `azure-identity`), lazy-imported so the other two backends never need them installed. Like
    DatabricksConnectAdapter, not exercised against a live database by this toolkit's own evals
    (see the mocked-connection tests in skills/data-discovery/evals/run_assertions.py for what IS
    covered without one) -- verify against a real (sandbox) SQL Server/Azure SQL Database before
    relying on it in an engagement.

`build_adapter()` below is the one place a caller picks between them, given an already-resolved
`backend` string (sqlite_fixture | databricks_connect) -- callers get that string from
toolkit.yaml's `environment.backend` themselves and pass it straight through; this module never
reads toolkit.yaml directly (see references/toolkit-conventions.md #2).

Every method that touches real row data (profile_column, sample_rows) takes an explicit
`limit`/`sample_size` -- callers (skills) are responsible for respecting toolkit.yaml's cost
and blast-radius thresholds *before* calling into this adapter; the adapter itself does not
know about toolkit.yaml.
"""
from __future__ import annotations

import abc
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_FORBIDDEN_SQL_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|MERGE|EXEC|EXECUTE|CALL)\b",
    re.IGNORECASE,
)


def assert_read_only_select(sql: str) -> None:
    """Defense-in-depth guard for execute_scalar (used by data-quality's custom_sql checks,
    the one place this toolkit runs a caller-supplied SQL string). Requires the statement to
    start with SELECT and contain none of the DDL/DML/admin keywords a write or schema change
    would need. This is NOT a substitute for connecting with a genuinely read-only credential --
    see references/toolkit-conventions.md #1 -- it's a second layer that fails loudly on an
    obviously-wrong check definition rather than a security boundary on its own.
    """
    stripped = sql.strip()
    if not re.match(r"^\s*SELECT\b", stripped, re.IGNORECASE):
        raise ValueError(f"custom_sql checks must be a single SELECT statement. Got: {sql[:80]!r}")
    if _FORBIDDEN_SQL_KEYWORDS.search(stripped):
        match = _FORBIDDEN_SQL_KEYWORDS.search(stripped)
        raise ValueError(f"custom_sql checks may not contain '{match.group(0)}'. Got: {sql[:80]!r}")
    if ";" in stripped.rstrip(";"):
        raise ValueError("custom_sql checks must be a single statement (no ';' except optionally at the very end).")


@dataclass
class ColumnInfo:
    name: str
    type: str
    nullable: bool
    comment: str | None = None


@dataclass
class Constraints:
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[dict] = field(default_factory=list)  # {columns, ref_schema, ref_table, ref_columns}
    not_null: list[str] = field(default_factory=list)


@dataclass
class ColumnProfile:
    column: str
    total_rows: int
    sampled_rows: int
    null_count: int
    distinct_count: int
    min_value: Any
    max_value: Any


class LakehouseAdapter(abc.ABC):
    @abc.abstractmethod
    def list_tables(self, schema: str) -> list[str]: ...

    @abc.abstractmethod
    def get_columns(self, schema: str, table: str) -> list[ColumnInfo]: ...

    @abc.abstractmethod
    def get_constraints(self, schema: str, table: str) -> Constraints: ...

    @abc.abstractmethod
    def get_table_comment(self, schema: str, table: str) -> str | None: ...

    @abc.abstractmethod
    def row_count(self, schema: str, table: str, exact: bool = True) -> int: ...

    @abc.abstractmethod
    def estimate_bytes(self, schema: str, table: str) -> int: ...

    @abc.abstractmethod
    def profile_column(self, schema: str, table: str, column: str, sample_size: int | None = None) -> ColumnProfile: ...

    @abc.abstractmethod
    def sample_rows(self, schema: str, table: str, columns: list[str], limit: int) -> list[dict]: ...

    @abc.abstractmethod
    def fetch_rows(self, schema: str, table: str, columns: list[str],
                    order_by: list[str] | None = None, limit: int | None = None) -> list[dict]: ...

    @abc.abstractmethod
    def count_orphans(self, schema: str, table: str, column: str,
                       ref_schema: str, ref_table: str, ref_column: str,
                       sample_size: int | None = None) -> dict: ...

    @abc.abstractmethod
    def check_uniqueness(self, schema: str, table: str, columns: list[str],
                          sample_size: int | None = None) -> dict: ...

    @abc.abstractmethod
    def execute_scalar(self, schema: str, sql: str): ...


class SQLiteFixtureAdapter(LakehouseAdapter):
    """Fixture/eval backend. `lakehouse_dir` must contain one SQLite file per schema,
    e.g. bronze.db, silver.db, gold.db, as produced by fixtures/generate_fixtures.py.
    """

    def __init__(self, lakehouse_dir: str | Path, catalog: str = "acme_retail_dev"):
        self.lakehouse_dir = Path(lakehouse_dir)
        self.catalog = catalog
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self._attached: set[str] = set()

    def _ensure_attached(self, schema: str) -> None:
        if schema in self._attached:
            return
        db_path = self.lakehouse_dir / f"{schema}.db"
        if not db_path.exists():
            raise FileNotFoundError(
                f"No fixture schema database at {db_path}. Run fixtures/generate_fixtures.py first."
            )
        self.conn.execute(f"ATTACH DATABASE '{db_path}' AS {schema}")
        self._attached.add(schema)

    def list_tables(self, schema: str) -> list[str]:
        self._ensure_attached(schema)
        rows = self.conn.execute(
            f"SELECT name FROM {schema}.sqlite_master WHERE type='table' AND name NOT LIKE '\\_%' ESCAPE '\\'"
        ).fetchall()
        return [r["name"] for r in rows]

    def get_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        self._ensure_attached(schema)
        rows = self.conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
        comments = self._column_comments(schema, table)
        return [
            ColumnInfo(name=r["name"], type=r["type"], nullable=not bool(r["notnull"]),
                       comment=comments.get(r["name"]))
            for r in rows
        ]

    def _column_comments(self, schema: str, table: str) -> dict:
        self._ensure_attached(schema)
        try:
            rows = self.conn.execute(
                f"SELECT column_name, comment FROM {schema}._column_comments WHERE table_name = ?",
                (table,),
            ).fetchall()
            return {r["column_name"]: r["comment"] for r in rows}
        except sqlite3.OperationalError:
            return {}

    def get_table_comment(self, schema: str, table: str) -> str | None:
        self._ensure_attached(schema)
        try:
            row = self.conn.execute(
                f"SELECT comment FROM {schema}._table_comments WHERE table_name = ?", (table,)
            ).fetchone()
            return row["comment"] if row else None
        except sqlite3.OperationalError:
            return None

    def get_constraints(self, schema: str, table: str) -> Constraints:
        self._ensure_attached(schema)
        pk_rows = self.conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
        pk = [r["name"] for r in sorted((r for r in pk_rows if r["pk"]), key=lambda r: r["pk"])]
        not_null = [r["name"] for r in pk_rows if r["notnull"]]
        fk_rows = self.conn.execute(f"PRAGMA {schema}.foreign_key_list({table})").fetchall()
        fks = []
        for r in fk_rows:
            fks.append({
                "columns": [r["from"]],
                "ref_schema": schema,  # fixtures only declare same-schema FKs
                "ref_table": r["table"],
                "ref_columns": [r["to"]],
            })
        return Constraints(primary_key=pk, foreign_keys=fks, not_null=not_null)

    def row_count(self, schema: str, table: str, exact: bool = True) -> int:
        self._ensure_attached(schema)
        row = self.conn.execute(f"SELECT COUNT(*) AS n FROM {schema}.{table}").fetchone()
        return row["n"]

    def estimate_bytes(self, schema: str, table: str) -> int:
        # Cheap, no-full-read estimate: sample a handful of rows, measure their repr size,
        # multiply by row count. Mirrors the spirit of a Databricks DESCRIBE DETAIL estimate
        # without needing storage statistics SQLite doesn't track.
        self._ensure_attached(schema)
        sample = self.conn.execute(f"SELECT * FROM {schema}.{table} LIMIT 50").fetchall()
        if not sample:
            return 0
        avg_row_bytes = sum(len(repr(tuple(r))) for r in sample) / len(sample)
        return int(avg_row_bytes * self.row_count(schema, table))

    def profile_column(self, schema: str, table: str, column: str, sample_size: int | None = None) -> ColumnProfile:
        self._ensure_attached(schema)
        total = self.row_count(schema, table)
        src = f"(SELECT {column} FROM {schema}.{table} LIMIT {sample_size})" if sample_size else f"{schema}.{table}"
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n, "
            f"SUM(CASE WHEN {column} IS NULL THEN 1 ELSE 0 END) AS nulls, "
            f"COUNT(DISTINCT {column}) AS distinct_n, "
            f"MIN({column}) AS min_v, MAX({column}) AS max_v FROM {src}"
        ).fetchone()
        return ColumnProfile(
            column=column, total_rows=total, sampled_rows=row["n"],
            null_count=row["nulls"] or 0, distinct_count=row["distinct_n"] or 0,
            min_value=row["min_v"], max_value=row["max_v"],
        )

    def sample_rows(self, schema: str, table: str, columns: list[str], limit: int) -> list[dict]:
        self._ensure_attached(schema)
        cols = ", ".join(columns)
        rows = self.conn.execute(f"SELECT {cols} FROM {schema}.{table} LIMIT {limit}").fetchall()
        return [dict(r) for r in rows]

    def fetch_rows(self, schema: str, table: str, columns: list[str],
                    order_by: list[str] | None = None, limit: int | None = None) -> list[dict]:
        self._ensure_attached(schema)
        cols = ", ".join(columns)
        sql = f"SELECT {cols} FROM {schema}.{table}"
        if order_by:
            sql += " ORDER BY " + ", ".join(order_by)
        if limit is not None:
            sql += f" LIMIT {limit}"
        rows = self.conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def count_orphans(self, schema: str, table: str, column: str,
                       ref_schema: str, ref_table: str, ref_column: str,
                       sample_size: int | None = None) -> dict:
        self._ensure_attached(schema)
        self._ensure_attached(ref_schema)
        src = f"(SELECT * FROM {schema}.{table} LIMIT {sample_size})" if sample_size else f"{schema}.{table}"
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n, "
            f"SUM(CASE WHEN t.{column} IS NOT NULL AND t.{column} NOT IN "
            f"(SELECT {ref_column} FROM {ref_schema}.{ref_table}) THEN 1 ELSE 0 END) AS orphans "
            f"FROM {src} AS t"
        ).fetchone()
        checked = row["n"] or 0
        orphans = row["orphans"] or 0
        return {"rows_checked": checked, "orphan_count": orphans,
                "orphan_rate": (orphans / checked) if checked else 0.0}

    def check_uniqueness(self, schema: str, table: str, columns: list[str],
                          sample_size: int | None = None) -> dict:
        self._ensure_attached(schema)
        cols = ", ".join(columns)
        src = f"(SELECT * FROM {schema}.{table} LIMIT {sample_size})" if sample_size else f"{schema}.{table}"
        not_null_clause = " AND ".join(f"{c} IS NOT NULL" for c in columns)
        # Standard SQL UNIQUE-constraint semantics: NULL is never considered equal to NULL, so
        # rows with a NULL in any key column don't count as duplicates of each other. SQLite's
        # COUNT(DISTINCT x) already ignores NULLs for a single column, but SELECT DISTINCT over
        # multiple columns does NOT (it groups all-NULL combinations together), so for the
        # composite case we explicitly exclude rows with any NULL key column before checking
        # distinctness, and report how many rows were excluded that way separately -- that's a
        # real nullability signal in its own right, not silently dropped.
        row = self.conn.execute(
            f"SELECT (SELECT COUNT(*) FROM {src}) AS n, "
            f"(SELECT COUNT(*) FROM {src} WHERE {not_null_clause}) AS non_null_n, "
            f"(SELECT COUNT(*) FROM (SELECT DISTINCT {cols} FROM {src} WHERE {not_null_clause})) AS distinct_n"
        ).fetchone()
        total = row["n"] or 0
        non_null = row["non_null_n"] or 0
        distinct = row["distinct_n"] or 0
        return {"rows_checked": total, "rows_with_null_key": total - non_null,
                "distinct_count": distinct, "is_unique": (non_null - distinct) == 0,
                "duplicate_count": non_null - distinct}

    def execute_scalar(self, schema: str, sql: str):
        assert_read_only_select(sql)
        self._ensure_attached(schema)
        row = self.conn.execute(sql).fetchone()
        if row is None:
            return None
        return row[0]


class DatabricksConnectAdapter(LakehouseAdapter):
    """Production backend: Databricks Connect, native catalog.schema.table addressing, metadata
    from information_schema -- same queries a Databricks SQL warehouse would run, executed via a
    Spark session instead of a DBAPI connection. Constructed from toolkit.yaml's
    `environment.catalog`; auth is never this class's concern (see references/toolkit-conventions.md
    #2) -- it either accepts an existing SparkSession or calls
    `DatabricksSession.builder.getOrCreate()`, which assumes Databricks Connect is already
    configured and authenticated in the host environment (profile, OAuth, env vars -- whatever the
    project's own Databricks Connect setup already provides). This class does not read toolkit.yaml,
    a secret store, or perform any auth/token-exchange step itself.

    Not exercised by this toolkit's automated evals (they run against SQLiteFixtureAdapter,
    offline). Implemented against the documented Databricks Connect and information_schema APIs;
    validate against a real workspace before relying on it in a new environment shape (e.g. Hive
    Metastore instead of Unity Catalog changes some information_schema behavior -- see references/
    for known differences).
    """

    def __init__(self, catalog: str, spark=None):
        if spark is not None:
            self.spark = spark
        else:
            try:
                from databricks.connect import DatabricksSession
            except ImportError as e:
                raise ImportError(
                    "DatabricksConnectAdapter requires the 'databricks-connect' package. "
                    "Install with: pip install databricks-connect"
                ) from e
            self.spark = DatabricksSession.builder.getOrCreate()
        self.catalog = catalog

    def _query(self, sql: str, **params) -> list[dict]:
        # Must go through the `args=` dict, not **kwargs -- spark.sql()'s **kwargs does Python-
        # string-style {name} substitution, not `:name` SQL-literal parameter binding. Only `args=`
        # binds the `:catalog`/`:schema`/`:table` placeholders used throughout this class.
        df = self.spark.sql(sql, args=params) if params else self.spark.sql(sql)
        return [row.asDict() for row in df.collect()]

    def list_tables(self, schema: str) -> list[str]:
        # Unity Catalog's information_schema is per-catalog, not global -- an unqualified
        # `information_schema.tables` resolves against current_catalog(), which is whatever the
        # session's default catalog is, NOT necessarily self.catalog. Every information_schema
        # query below must be qualified with `{self.catalog}.information_schema....` or it silently
        # returns zero rows the moment self.catalog isn't the session default (caught by testing
        # against a real workspace with samples as the target catalog, not the session default).
        rows = self._query(
            f"SELECT table_name FROM {self.catalog}.information_schema.tables "
            "WHERE table_schema = :schema",
            schema=schema,
        )
        return [r["table_name"] for r in rows]

    def get_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        rows = self._query(
            f"SELECT column_name, full_data_type, is_nullable, comment FROM {self.catalog}.information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table "
            "ORDER BY ordinal_position",
            schema=schema, table=table,
        )
        return [ColumnInfo(name=r["column_name"], type=r["full_data_type"],
                            nullable=(r["is_nullable"] == "YES"), comment=r.get("comment"))
                for r in rows]

    def get_table_comment(self, schema: str, table: str) -> str | None:
        rows = self._query(
            f"SELECT comment FROM {self.catalog}.information_schema.tables "
            "WHERE table_schema = :schema AND table_name = :table",
            schema=schema, table=table,
        )
        return rows[0]["comment"] if rows else None

    def get_constraints(self, schema: str, table: str) -> Constraints:
        pk_rows = self._query(
            f"SELECT kcu.column_name FROM {self.catalog}.information_schema.key_column_usage kcu "
            f"JOIN {self.catalog}.information_schema.table_constraints tc ON kcu.constraint_name = tc.constraint_name "
            "WHERE tc.constraint_type = 'PRIMARY KEY' "
            "AND tc.table_schema = :schema AND tc.table_name = :table ORDER BY kcu.ordinal_position",
            schema=schema, table=table,
        )
        fk_rows = self._query(
            f"SELECT kcu.column_name, ccu.table_schema AS ref_schema, ccu.table_name AS ref_table, "
            f"ccu.column_name AS ref_column FROM {self.catalog}.information_schema.key_column_usage kcu "
            f"JOIN {self.catalog}.information_schema.table_constraints tc ON kcu.constraint_name = tc.constraint_name "
            f"JOIN {self.catalog}.information_schema.constraint_column_usage ccu ON tc.constraint_name = ccu.constraint_name "
            "WHERE tc.constraint_type = 'FOREIGN KEY' "
            "AND tc.table_schema = :schema AND tc.table_name = :table",
            schema=schema, table=table,
        )
        cols = self.get_columns(schema, table)
        not_null = [c.name for c in cols if not c.nullable]
        return Constraints(
            primary_key=[r["column_name"] for r in pk_rows],
            foreign_keys=[{"columns": [r["column_name"]], "ref_schema": r["ref_schema"],
                            "ref_table": r["ref_table"], "ref_columns": [r["ref_column"]]} for r in fk_rows],
            not_null=not_null,
        )

    def row_count(self, schema: str, table: str, exact: bool = True) -> int:
        if not exact:
            rows = self._query(f"DESCRIBE DETAIL {self.catalog}.{schema}.{table}")
            if rows and rows[0].get("numFiles") is not None:
                # numRows not always populated pre-scan; caller should treat non-exact as approximate.
                pass
        rows = self._query(f"SELECT COUNT(*) AS n FROM {self.catalog}.{schema}.{table}")
        return rows[0]["n"]

    def estimate_bytes(self, schema: str, table: str) -> int:
        rows = self._query(f"DESCRIBE DETAIL {self.catalog}.{schema}.{table}")
        return int(rows[0]["sizeInBytes"]) if rows and rows[0].get("sizeInBytes") is not None else 0

    def profile_column(self, schema: str, table: str, column: str, sample_size: int | None = None) -> ColumnProfile:
        total = self.row_count(schema, table)
        src = f"(SELECT {column} FROM {self.catalog}.{schema}.{table} TABLESAMPLE ({sample_size} ROWS))" \
            if sample_size else f"{self.catalog}.{schema}.{table}"
        rows = self._query(
            f"SELECT COUNT(*) AS n, SUM(CASE WHEN {column} IS NULL THEN 1 ELSE 0 END) AS nulls, "
            f"COUNT(DISTINCT {column}) AS distinct_n, MIN({column}) AS min_v, MAX({column}) AS max_v FROM {src}"
        )
        r = rows[0]
        return ColumnProfile(column=column, total_rows=total, sampled_rows=r["n"],
                              null_count=r["nulls"] or 0, distinct_count=r["distinct_n"] or 0,
                              min_value=r["min_v"], max_value=r["max_v"])

    def sample_rows(self, schema: str, table: str, columns: list[str], limit: int) -> list[dict]:
        cols = ", ".join(columns)
        return self._query(f"SELECT {cols} FROM {self.catalog}.{schema}.{table} LIMIT {limit}")

    def fetch_rows(self, schema: str, table: str, columns: list[str],
                    order_by: list[str] | None = None, limit: int | None = None) -> list[dict]:
        cols = ", ".join(columns)
        sql = f"SELECT {cols} FROM {self.catalog}.{schema}.{table}"
        if order_by:
            sql += " ORDER BY " + ", ".join(order_by)
        if limit is not None:
            sql += f" LIMIT {limit}"
        return self._query(sql)

    def count_orphans(self, schema: str, table: str, column: str,
                       ref_schema: str, ref_table: str, ref_column: str,
                       sample_size: int | None = None) -> dict:
        src = f"(SELECT * FROM {self.catalog}.{schema}.{table} TABLESAMPLE ({sample_size} ROWS))" \
            if sample_size else f"{self.catalog}.{schema}.{table}"
        rows = self._query(
            f"SELECT COUNT(*) AS n, SUM(CASE WHEN t.{column} IS NOT NULL AND t.{column} NOT IN "
            f"(SELECT {ref_column} FROM {self.catalog}.{ref_schema}.{ref_table}) THEN 1 ELSE 0 END) AS orphans "
            f"FROM {src} AS t"
        )
        r = rows[0]
        checked = r["n"] or 0
        orphans = r["orphans"] or 0
        return {"rows_checked": checked, "orphan_count": orphans,
                "orphan_rate": (orphans / checked) if checked else 0.0}

    def check_uniqueness(self, schema: str, table: str, columns: list[str],
                          sample_size: int | None = None) -> dict:
        cols = ", ".join(columns)
        src = f"(SELECT * FROM {self.catalog}.{schema}.{table} TABLESAMPLE ({sample_size} ROWS))" \
            if sample_size else f"{self.catalog}.{schema}.{table}"
        not_null_clause = " AND ".join(f"{c} IS NOT NULL" for c in columns)
        # Same NULL handling as SQLiteFixtureAdapter: rows with a NULL in any key column are
        # excluded from the distinctness check (standard UNIQUE-constraint semantics), not
        # collapsed together as if they were duplicates of each other.
        rows = self._query(
            f"SELECT (SELECT COUNT(*) FROM {src}) AS n, "
            f"(SELECT COUNT(*) FROM {src} WHERE {not_null_clause}) AS non_null_n, "
            f"(SELECT COUNT(DISTINCT {cols}) FROM {src} WHERE {not_null_clause}) AS distinct_n"
        )
        r = rows[0]
        total = r["n"] or 0
        non_null = r["non_null_n"] or 0
        distinct = r["distinct_n"] or 0
        return {"rows_checked": total, "rows_with_null_key": total - non_null,
                "distinct_count": distinct, "is_unique": (non_null - distinct) == 0,
                "duplicate_count": non_null - distinct}

    def execute_scalar(self, schema: str, sql: str):
        assert_read_only_select(sql)
        rows = self._query(sql)
        if not rows:
            return None
        first_row = rows[0]
        return next(iter(first_row.values()))


class SqlServerAdapter(LakehouseAdapter):
    """Pre-ingestion backend: profiles a SQL Server database directly, before anything is landed
    in the lakehouse -- see skills/data-discovery/references/sqlserver-profiling.md for the
    workflow this feeds into (a bronze-landing data-contract.json that data-pipeline's existing
    source_is_managed_connector rubric already routes to lakeflow_connect, no changes needed
    there). `database` plays the same role `catalog` plays on the other two adapters.

    Auth is ambient, same posture as DatabricksConnectAdapter (references/toolkit-conventions.md
    #2): this class never reads toolkit.yaml or a secret store itself. Three modes, chosen via
    `auth_mode`:
      - "azure_ad_default" (recommended when the SQL Server is Azure SQL Database/Managed
        Instance, the natural parallel to Databricks Connect's own OAuth session): fetches a
        token from whatever's already logged in (Azure CLI, managed identity, env-based service
        principal) via azure-identity's DefaultAzureCredential. No username/password at all.
      - "sql_auth_env": reads the username/password from the environment variables NAMED by
        `username_env_var`/`password_env_var` (which come from toolkit.yaml -- never the values
        themselves). Halts with a clear message naming the missing variable if either isn't set,
        per toolkit-conventions.md #2's "halt, name the missing key" rule -- never guesses or
        prompts.
      - "windows_integrated": trusted connection, no credential material at all -- for an on-prem
        SQL Server where the host machine's domain identity already has access.

    Requires `pyodbc` (lazy-imported, same pattern DatabricksConnectAdapter uses for
    databricks.connect) plus the Microsoft ODBC Driver for SQL Server installed at the OS level --
    a real, non-Python environment dependency, not just a pip install. `azure_ad_default` also
    needs `azure-identity`. Accepts a pre-built `conn` (a pyodbc-Connection-shaped object) for
    testing without either package or a real server -- see
    skills/data-discovery/evals/run_assertions.py's mocked-connection tests, the only coverage
    this class gets without a live SQL Server (same limitation DatabricksConnectAdapter has always
    had; verify against a real sandbox instance before relying on this in an engagement).

    Structurally read-only like the other two adapters (LakehouseAdapter has no write method at
    all) -- but the real security boundary is the credential itself being a database-level
    read-only login, same caveat assert_read_only_select's own docstring already states for
    execute_scalar: an app-level guard is defense-in-depth, not the boundary on its own.
    """

    def __init__(self, host: str, database: str, driver: str = "ODBC Driver 18 for SQL Server",
                 port: int = 1433, auth_mode: str = "azure_ad_default",
                 username_env_var: str | None = None, password_env_var: str | None = None,
                 conn=None):
        self.database = database
        if conn is not None:
            self.conn = conn
            return

        # Validate auth_mode/config BEFORE importing pyodbc -- a toolkit.yaml misconfiguration
        # (unknown auth_mode, a missing env var name) should fail with a clear message regardless
        # of whether pyodbc happens to be installed yet, not be masked by an unrelated ImportError.
        username = password = token = None
        if auth_mode == "sql_auth_env":
            if not username_env_var or not password_env_var:
                raise ValueError(
                    "auth_mode 'sql_auth_env' requires both username_env_var and password_env_var "
                    "to be set in toolkit.yaml's external_sources.sqlserver block -- names of "
                    "environment variables to read, never the credential values themselves."
                )
            username = os.environ.get(username_env_var)
            password = os.environ.get(password_env_var)
            if username is None:
                raise ValueError(
                    f"toolkit.yaml's external_sources.sqlserver.username_env_var names "
                    f"'{username_env_var}', but that environment variable is not set."
                )
            if password is None:
                raise ValueError(
                    f"toolkit.yaml's external_sources.sqlserver.password_env_var names "
                    f"'{password_env_var}', but that environment variable is not set."
                )
        elif auth_mode == "azure_ad_default":
            try:
                from azure.identity import DefaultAzureCredential
            except ImportError as e:
                raise ImportError(
                    "auth_mode 'azure_ad_default' requires the 'azure-identity' package. "
                    "Install with: pip install azure-identity"
                ) from e
            token = DefaultAzureCredential().get_token("https://database.windows.net/.default").token
        elif auth_mode != "windows_integrated":
            raise ValueError(
                f"Unknown auth_mode '{auth_mode}' -- expected 'azure_ad_default', 'sql_auth_env', "
                "or 'windows_integrated'."
            )

        try:
            import pyodbc
        except ImportError as e:
            raise ImportError(
                "SqlServerAdapter requires the 'pyodbc' package, plus the Microsoft ODBC Driver "
                "for SQL Server installed at the OS level (pyodbc alone is not enough). "
                "Install with: pip install pyodbc"
            ) from e

        conn_str = (
            f"DRIVER={{{driver}}};SERVER={host},{port};DATABASE={database};"
            "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
        )

        if auth_mode == "azure_ad_default":
            # pyodbc's documented shape for SQL_COPT_SS_ACCESS_TOKEN (1256): the token
            # UTF-16-LE-encoded and length-prefixed as a little-endian 4-byte struct -- this exact
            # encoding is pyodbc's own requirement for passing an AAD token, not this toolkit's
            # invention.
            token_bytes = token.encode("utf-16-le")
            token_struct = len(token_bytes).to_bytes(4, "little") + token_bytes
            self.conn = pyodbc.connect(conn_str, attrs_before={1256: token_struct})
        elif auth_mode == "sql_auth_env":
            self.conn = pyodbc.connect(conn_str + f"UID={username};PWD={password};")
        else:
            self.conn = pyodbc.connect(conn_str + "Trusted_Connection=yes;")

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        cursor = self.conn.cursor()
        cursor.execute(sql, params)
        if cursor.description is None:
            return []
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    @staticmethod
    def _full_type(row: dict) -> str:
        # Mirrors this toolkit's "decimal(18,2)"-style type naming elsewhere where SQL Server's
        # own precision/scale metadata makes that possible; falls back to the bare DATA_TYPE
        # otherwise (e.g. INT, DATE) -- a documented simplification, not a silent guess.
        base = row["DATA_TYPE"]
        max_len = row.get("CHARACTER_MAXIMUM_LENGTH")
        if max_len not in (None, -1):
            return f"{base}({max_len})"
        if base in ("decimal", "numeric") and row.get("NUMERIC_PRECISION") is not None:
            return f"{base}({row['NUMERIC_PRECISION']},{row['NUMERIC_SCALE']})"
        return base

    def list_tables(self, schema: str) -> list[str]:
        rows = self._query(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = ? AND TABLE_TYPE = 'BASE TABLE'",
            (schema,),
        )
        return [r["TABLE_NAME"] for r in rows]

    def get_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        # LEFT JOIN sys.extended_properties for MS_Description -- SQL Server's equivalent of a
        # Unity Catalog column comment, stored as an extended property, not a first-class
        # INFORMATION_SCHEMA field.
        rows = self._query(
            "SELECT c.COLUMN_NAME, c.DATA_TYPE, c.IS_NULLABLE, c.CHARACTER_MAXIMUM_LENGTH, "
            "c.NUMERIC_PRECISION, c.NUMERIC_SCALE, "
            "CAST(ep.value AS NVARCHAR(MAX)) AS column_comment "
            "FROM INFORMATION_SCHEMA.COLUMNS c "
            "JOIN sys.tables t ON t.name = c.TABLE_NAME "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id AND s.name = c.TABLE_SCHEMA "
            "LEFT JOIN sys.extended_properties ep "
            "  ON ep.major_id = t.object_id "
            "  AND ep.minor_id = COLUMNPROPERTY(t.object_id, c.COLUMN_NAME, 'ColumnId') "
            "  AND ep.name = 'MS_Description' "
            "WHERE c.TABLE_SCHEMA = ? AND c.TABLE_NAME = ? "
            "ORDER BY c.ORDINAL_POSITION",
            (schema, table),
        )
        return [
            ColumnInfo(name=r["COLUMN_NAME"], type=self._full_type(r),
                       nullable=(r["IS_NULLABLE"] == "YES"), comment=r.get("column_comment"))
            for r in rows
        ]

    def get_table_comment(self, schema: str, table: str) -> str | None:
        rows = self._query(
            "SELECT CAST(ep.value AS NVARCHAR(MAX)) AS comment "
            "FROM sys.tables t "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "LEFT JOIN sys.extended_properties ep "
            "  ON ep.major_id = t.object_id AND ep.minor_id = 0 AND ep.name = 'MS_Description' "
            "WHERE s.name = ? AND t.name = ?",
            (schema, table),
        )
        return rows[0]["comment"] if rows else None

    def get_constraints(self, schema: str, table: str) -> Constraints:
        pk_rows = self._query(
            "SELECT kcu.COLUMN_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu "
            "JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc ON kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME "
            "WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY' "
            "AND tc.TABLE_SCHEMA = ? AND tc.TABLE_NAME = ? ORDER BY kcu.ORDINAL_POSITION",
            (schema, table),
        )
        # REFERENTIAL_CONSTRAINTS -> CONSTRAINT_COLUMN_USAGE is SQL Server's documented,
        # version-stable way to map an FK constraint to the column it actually references (the
        # unique/PK constraint on the other side) -- sys.foreign_key_columns is an alternative but
        # this stays consistent with the INFORMATION_SCHEMA-first style DatabricksConnectAdapter
        # already uses for the same query shape.
        fk_rows = self._query(
            "SELECT kcu.COLUMN_NAME, ccu.TABLE_SCHEMA AS ref_schema, ccu.TABLE_NAME AS ref_table, "
            "ccu.COLUMN_NAME AS ref_column "
            "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu "
            "JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc ON kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME "
            "JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc ON tc.CONSTRAINT_NAME = rc.CONSTRAINT_NAME "
            "JOIN INFORMATION_SCHEMA.CONSTRAINT_COLUMN_USAGE ccu ON rc.UNIQUE_CONSTRAINT_NAME = ccu.CONSTRAINT_NAME "
            "WHERE tc.CONSTRAINT_TYPE = 'FOREIGN KEY' AND tc.TABLE_SCHEMA = ? AND tc.TABLE_NAME = ?",
            (schema, table),
        )
        cols = self.get_columns(schema, table)
        not_null = [c.name for c in cols if not c.nullable]
        return Constraints(
            primary_key=[r["COLUMN_NAME"] for r in pk_rows],
            foreign_keys=[{"columns": [r["COLUMN_NAME"]], "ref_schema": r["ref_schema"],
                            "ref_table": r["ref_table"], "ref_columns": [r["ref_column"]]} for r in fk_rows],
            not_null=not_null,
        )

    def row_count(self, schema: str, table: str, exact: bool = True) -> int:
        if not exact:
            # sys.partitions' row count for the heap/clustered index (index_id IN (0,1)) is a
            # cheap, no-full-scan estimate -- the standard SQL Server trick sp_spaceused itself
            # uses internally, mirroring estimate_bytes' "cheap, no-full-read" contract.
            rows = self._query(
                "SELECT SUM(p.rows) AS n FROM sys.partitions p "
                "JOIN sys.tables t ON p.object_id = t.object_id "
                "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "WHERE s.name = ? AND t.name = ? AND p.index_id IN (0, 1)",
                (schema, table),
            )
            if rows and rows[0]["n"] is not None:
                return int(rows[0]["n"])
        rows = self._query(f"SELECT COUNT(*) AS n FROM [{schema}].[{table}]")
        return rows[0]["n"]

    def estimate_bytes(self, schema: str, table: str) -> int:
        rows = self._query(
            "SELECT SUM(ps.used_page_count) AS pages FROM sys.dm_db_partition_stats ps "
            "JOIN sys.tables t ON ps.object_id = t.object_id "
            "JOIN sys.schemas s ON t.schema_id = s.schema_id "
            "WHERE s.name = ? AND t.name = ? AND ps.index_id IN (0, 1)",
            (schema, table),
        )
        pages = rows[0]["pages"] if rows and rows[0]["pages"] is not None else 0
        return int(pages) * 8 * 1024  # SQL Server pages are a fixed 8 KiB

    # T-SQL rejects MIN/MAX on `bit`, and rejects COUNT(DISTINCT ...)/MIN/MAX outright on these
    # large-object/special types (error 8117 either way) -- checked against the declared type
    # rather than guessed-and-retried, since the exact set is fixed and documented. sql_variant/
    # rowversion/timestamp are included speculatively (same documented restriction, not yet
    # reproduced against a live server) -- worst case they return NULL where MIN/MAX would have
    # actually worked, not a crash.
    _NO_MIN_MAX_TYPES = {"bit", "rowversion", "timestamp"}
    _NO_AGGREGATE_TYPES = {"xml", "text", "ntext", "image", "geography", "geometry",
                            "hierarchyid", "sql_variant"}

    def profile_column(self, schema: str, table: str, column: str, sample_size: int | None = None) -> ColumnProfile:
        total = self.row_count(schema, table)
        src = f"(SELECT TOP ({sample_size}) {column} FROM [{schema}].[{table}]) AS s" if sample_size \
            else f"[{schema}].[{table}]"
        type_rows = self._query(
            "SELECT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND COLUMN_NAME = ?",
            (schema, table, column),
        )
        data_type = type_rows[0]["DATA_TYPE"] if type_rows else None
        no_aggregate = data_type in self._NO_AGGREGATE_TYPES
        no_min_max = no_aggregate or data_type in self._NO_MIN_MAX_TYPES
        distinct_select = "NULL AS distinct_n" if no_aggregate \
            else f"COUNT(DISTINCT {column}) AS distinct_n"
        min_max_select = "NULL AS min_v, NULL AS max_v" if no_min_max \
            else f"MIN({column}) AS min_v, MAX({column}) AS max_v"
        rows = self._query(
            f"SELECT COUNT(*) AS n, SUM(CASE WHEN {column} IS NULL THEN 1 ELSE 0 END) AS nulls, "
            f"{distinct_select}, {min_max_select} "
            f"FROM {src}"
        )
        r = rows[0]
        return ColumnProfile(column=column, total_rows=total, sampled_rows=r["n"],
                              null_count=r["nulls"] or 0, distinct_count=r["distinct_n"] or 0,
                              min_value=r["min_v"], max_value=r["max_v"])

    def sample_rows(self, schema: str, table: str, columns: list[str], limit: int) -> list[dict]:
        cols = ", ".join(columns)
        return self._query(f"SELECT TOP ({limit}) {cols} FROM [{schema}].[{table}]")

    def fetch_rows(self, schema: str, table: str, columns: list[str],
                    order_by: list[str] | None = None, limit: int | None = None) -> list[dict]:
        cols = ", ".join(columns)
        top_clause = f"TOP ({limit}) " if limit is not None else ""
        sql = f"SELECT {top_clause}{cols} FROM [{schema}].[{table}]"
        if order_by:
            sql += " ORDER BY " + ", ".join(order_by)
        return self._query(sql)

    def count_orphans(self, schema: str, table: str, column: str,
                       ref_schema: str, ref_table: str, ref_column: str,
                       sample_size: int | None = None) -> dict:
        src = f"(SELECT TOP ({sample_size}) * FROM [{schema}].[{table}])" if sample_size \
            else f"[{schema}].[{table}]"
        # NOT EXISTS, not NOT IN -- T-SQL's well-known NOT IN + NULL footgun (if the subquery ever
        # returns any NULL, NOT IN evaluates to UNKNOWN for every row, silently reporting zero
        # orphans regardless of the real answer). NOT EXISTS has no such trap.
        #
        # The flag is computed in an inner derived table and SUM'd in an outer query, rather than
        # SUM(CASE WHEN ... EXISTS(subquery) ... END) directly -- T-SQL (confirmed against a live
        # Azure SQL DB, compat level 170) rejects that shape outright with error 130 ("Cannot
        # perform an aggregate function on an expression containing an aggregate or a subquery"),
        # regardless of EXISTS vs. NOT EXISTS.
        rows = self._query(
            f"SELECT SUM(flag) AS orphans, COUNT(*) AS n FROM ("
            f"SELECT CASE WHEN t.{column} IS NOT NULL AND NOT EXISTS "
            f"(SELECT 1 FROM [{ref_schema}].[{ref_table}] r WHERE r.{ref_column} = t.{column}) "
            f"THEN 1 ELSE 0 END AS flag FROM {src} AS t) AS d"
        )
        r = rows[0]
        checked = r["n"] or 0
        orphans = r["orphans"] or 0
        return {"rows_checked": checked, "orphan_count": orphans,
                "orphan_rate": (orphans / checked) if checked else 0.0}

    def check_uniqueness(self, schema: str, table: str, columns: list[str],
                          sample_size: int | None = None) -> dict:
        cols = ", ".join(columns)
        src = f"(SELECT TOP ({sample_size}) * FROM [{schema}].[{table}]) AS s" if sample_size \
            else f"[{schema}].[{table}]"
        not_null_clause = " AND ".join(f"{c} IS NOT NULL" for c in columns)
        # Same NULL handling as the other two adapters: rows with a NULL in any key column are
        # excluded from the distinctness check (standard UNIQUE-constraint semantics), not
        # collapsed together as if they were duplicates of each other.
        rows = self._query(
            f"SELECT (SELECT COUNT(*) FROM {src}) AS n, "
            f"(SELECT COUNT(*) FROM {src} WHERE {not_null_clause}) AS non_null_n, "
            f"(SELECT COUNT(*) FROM (SELECT DISTINCT {cols} FROM {src} WHERE {not_null_clause}) AS d) AS distinct_n"
        )
        r = rows[0]
        total = r["n"] or 0
        non_null = r["non_null_n"] or 0
        distinct = r["distinct_n"] or 0
        return {"rows_checked": total, "rows_with_null_key": total - non_null,
                "distinct_count": distinct, "is_unique": (non_null - distinct) == 0,
                "duplicate_count": non_null - distinct}

    def execute_scalar(self, schema: str, sql: str):
        assert_read_only_select(sql)
        rows = self._query(sql)
        if not rows:
            return None
        return next(iter(rows[0].values()))


def build_adapter(backend: str, lakehouse_dir: str | Path | None = None,
                   catalog: str = "acme_retail_dev", spark=None,
                   sqlserver_host: str | None = None, sqlserver_database: str | None = None,
                   sqlserver_driver: str = "ODBC Driver 18 for SQL Server",
                   sqlserver_port: int = 1433, sqlserver_auth_mode: str = "azure_ad_default",
                   sqlserver_username_env_var: str | None = None,
                   sqlserver_password_env_var: str | None = None,
                   sqlserver_conn=None) -> LakehouseAdapter:
    """Instantiates the right backend given an already-resolved `backend` value (sqlite_fixture |
    databricks_connect | sqlserver). Callers get that string, and every sqlserver_* value, from
    toolkit.yaml themselves (environment.backend, or external_sources.sqlserver for the sqlserver
    case) and pass it straight through -- this function never reads toolkit.yaml directly (see
    references/toolkit-conventions.md #2). Note sqlserver_username_env_var/password_env_var are
    environment variable NAMES, never credential values -- SqlServerAdapter resolves the actual
    values itself, at connection time, from os.environ.
    """
    if backend == "sqlite_fixture":
        if lakehouse_dir is None:
            raise ValueError("backend 'sqlite_fixture' requires lakehouse_dir")
        return SQLiteFixtureAdapter(lakehouse_dir, catalog=catalog)
    if backend == "databricks_connect":
        return DatabricksConnectAdapter(catalog=catalog, spark=spark)
    if backend == "sqlserver":
        if sqlserver_host is None:
            raise ValueError("backend 'sqlserver' requires sqlserver_host")
        if sqlserver_database is None:
            raise ValueError("backend 'sqlserver' requires sqlserver_database")
        return SqlServerAdapter(
            host=sqlserver_host, database=sqlserver_database, driver=sqlserver_driver,
            port=sqlserver_port, auth_mode=sqlserver_auth_mode,
            username_env_var=sqlserver_username_env_var, password_env_var=sqlserver_password_env_var,
            conn=sqlserver_conn,
        )
    raise ValueError(f"Unknown backend '{backend}' -- expected 'sqlite_fixture', 'databricks_connect', or 'sqlserver'.")
