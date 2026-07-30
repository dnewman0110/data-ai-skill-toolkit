#!/usr/bin/env python3
"""
lakehouse_adapter.py -- backend-agnostic interface every skill's deterministic scripts use to
talk to "the lakehouse", so the same profiling/scan code runs against the local synthetic
fixture (offline, in evals/CI) and a real Databricks/Unity Catalog workspace (in production)
without a single skill script knowing which one it's talking to.

Two backends:
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


def build_adapter(backend: str, lakehouse_dir: str | Path | None = None,
                   catalog: str = "acme_retail_dev", spark=None) -> LakehouseAdapter:
    """Instantiates the right backend given an already-resolved `backend` value (sqlite_fixture |
    databricks_connect). Callers get that string from toolkit.yaml's `environment.backend`
    themselves and pass it straight through -- this function never reads toolkit.yaml directly
    (see references/toolkit-conventions.md #2).
    """
    if backend == "sqlite_fixture":
        if lakehouse_dir is None:
            raise ValueError("backend 'sqlite_fixture' requires lakehouse_dir")
        return SQLiteFixtureAdapter(lakehouse_dir, catalog=catalog)
    if backend == "databricks_connect":
        return DatabricksConnectAdapter(catalog=catalog, spark=spark)
    raise ValueError(f"Unknown backend '{backend}' -- expected 'sqlite_fixture' or 'databricks_connect'.")
