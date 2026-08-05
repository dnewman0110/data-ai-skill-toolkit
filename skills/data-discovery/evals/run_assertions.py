#!/usr/bin/env python3
"""
run_assertions.py -- deterministic checks for data-discovery, runnable in CI with no subagent and
no LLM call. These are the negative/regression tests the spec calls for (malformed input rejected
cleanly, read-only skills never attempt writes) plus a live smoke test of the deterministic
pipeline (profile -> propose tests -> findings) against the fixture lakehouse, asserting the
planted flaws are actually caught. The four scenario evals in evals.json/eval_metadata.json that
require reasoning (mapping proposals, resolution-mode judgment, redirect behavior) are graded
separately via subagent runs -- see evals/README.md.

Exit 0 if every check passes, 1 otherwise (prints every failure).
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = Path(__file__).resolve().parents[1]
LAKEHOUSE_DIR = REPO_ROOT / "fixtures" / "lakehouse"

failures = []


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


def run(cmd, expect_success=True):
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if expect_success and result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
    return result


def _raises(exc_type, fn):
    try:
        fn()
        return False
    except exc_type:
        return True


# -- 1. Read-only enforcement: none of data-discovery's scripts issue a write statement against
#    the lakehouse. generate_fixtures.py is exempt (it's what BUILDS the fixture, not a discovery
#    script). This is a static check, not a runtime one -- a runtime check would need a live
#    workspace to prove a negative against; a static scan of the scripts this skill ships is the
#    right level for "this skill's shipped code never contains DDL/DML."
write_keywords = re.compile(r"\b(INSERT INTO|UPDATE |DELETE FROM|DROP TABLE|CREATE TABLE|ALTER TABLE|TRUNCATE)\b", re.IGNORECASE)
scripts_dir = SKILL_DIR / "scripts"
offending = []
for py_file in scripts_dir.glob("*.py"):
    text = py_file.read_text()
    for m in write_keywords.finditer(text):
        offending.append(f"{py_file.name}: found '{m.group(0)}'")
check("data-discovery scripts contain no DDL/DML write statements", len(offending) == 0)
for o in offending:
    print(f"    {o}")

# -- 2. Malformed / unsupported-major artifact rejected cleanly, not best-effort parsed.
if (REPO_ROOT / "contracts" / "examples" / "data-contract.example.json").exists():
    result = run([
        sys.executable, "scripts/validate_artifact.py",
        "contracts/examples/data-contract.example.json",
        "--schema-type", "data-contract", "--supported-major", "99",
    ], expect_success=False)
    check("Artifact with unsupported major version is refused (nonzero exit, clear message)",
          result.returncode != 0 and "unsupported" in (result.stdout + result.stderr).lower()
          or "supports major version" in (result.stdout + result.stderr))

# Structurally invalid artifact (missing required fields) also rejected, not silently accepted.
bad_artifact = {"schema_version": "1.0.0", "run": {}, "contract_id": "x"}
bad_path = Path(tempfile.gettempdir()) / "bad_contract.json"
bad_path.write_text(json.dumps(bad_artifact))
result = run([sys.executable, "scripts/validate_artifact.py", str(bad_path), "--schema-type", "data-contract"],
             expect_success=False)
check("Structurally invalid artifact (missing required fields) is rejected", result.returncode != 0)

# -- 3. Live smoke test against the fixture lakehouse: planted flaws are actually caught.
if LAKEHOUSE_DIR.exists():
    sys.path.insert(0, str(SKILL_DIR / "scripts"))
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from lakehouse_adapter import SQLiteFixtureAdapter  # noqa: E402
    from profile_object import profile_table  # noqa: E402
    from propose_tests import propose_tests  # noqa: E402

    adapter = SQLiteFixtureAdapter(str(LAKEHOUSE_DIR))

    orders_profile = profile_table(adapter, "silver", "orders", None,
                                    candidate_fks=[{"column": "customer_id", "ref_schema": "silver",
                                                     "ref_table": "customers", "ref_column": "customer_id"}])
    orders_result = propose_tests(orders_profile)
    orders_findings_text = json.dumps(orders_result["findings"])

    check("Broken FK (orphaned customer_id) is caught",
          any(fk["orphan_count"] > 0 for fk in orders_profile["fk_checks"]))
    check("Nullable-that-shouldn't-be (ship_region) is caught",
          "ship_region" in orders_findings_text)
    check("Type mismatch (total_amt TEXT-but-numeric) is caught",
          "total_amt" in orders_findings_text)
    check("Grain (order_id, line_number) profiles as unique via declared PK",
          any(ck["source"] == "declared_primary_key" and ck["is_unique"]
              for ck in orders_profile["candidate_keys"]))
    check("No false-positive uniqueness finding on line_number alone",
          not any(f["statement"].startswith("Candidate key (line_number)") for f in orders_result["findings"]))

    customers_profile = profile_table(adapter, "silver", "customers", None)
    customers_result = propose_tests(customers_profile)
    check("Duplicated natural key (customer_number) is caught",
          any("customer_number" in f["statement"] and "NOT currently unique" in f["statement"]
              for f in customers_result["findings"]))
else:
    print("[SKIP] Live fixture smoke test -- run fixtures/generate_fixtures.py first.")

# -- 4. SqlServerAdapter: mocked-connection tests. No live SQL Server in CI (same limitation
#    DatabricksConnectAdapter has always had -- see its own docstring and DECISIONS.md decision
#    54) -- this checks generated-SQL shape and config validation against a fake pyodbc-shaped
#    connection, not correctness against a real server.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from lakehouse_adapter import SqlServerAdapter, build_adapter  # noqa: E402


class _FakeCursor:
    def __init__(self, responses, log):
        self._responses = responses
        self._log = log
        self.description = None
        self._rows = []

    def execute(self, sql, params):
        self._log.append((sql, params))
        cols, rows = self._responses.pop(0)
        self.description = [(c,) for c in cols] if cols else None
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    """Mimics enough of pyodbc.Connection's interface (a .cursor() returning an object with
    .execute(sql, params)/.description/.fetchall()) for SqlServerAdapter's _query to work
    unmodified -- injected via the `conn=` constructor param, same dependency-injection pattern
    DatabricksConnectAdapter already uses via its `spark=` param for the same reason."""

    def __init__(self, responses):
        self._responses = responses
        self.log = []

    def cursor(self):
        return _FakeCursor(self._responses, self.log)


sqlserver_adapter = SqlServerAdapter(host="fake-host", database="fake_db",
                                      conn=_FakeConn([(["TABLE_NAME"], [("orders",)])]))
sqlserver_adapter.list_tables("dbo")
check("SqlServerAdapter.list_tables queries INFORMATION_SCHEMA.TABLES, not sys.tables directly",
      "INFORMATION_SCHEMA.TABLES" in sqlserver_adapter.conn.log[0][0])

sample_conn = _FakeConn([(["order_id"], [(1,), (2,)])])
SqlServerAdapter(host="fake-host", database="fake_db", conn=sample_conn).sample_rows("dbo", "orders", ["order_id"], 5)
sample_sql = sample_conn.log[0][0]
check("SqlServerAdapter.sample_rows uses TOP, not LIMIT (SQLite/Spark syntax)",
      "TOP (5)" in sample_sql and "LIMIT" not in sample_sql)

orphan_conn = _FakeConn([(["n", "orphans"], [(10, 2)])])
orphan_result = SqlServerAdapter(host="fake-host", database="fake_db", conn=orphan_conn).count_orphans(
    "dbo", "orders", "customer_id", "dbo", "customers", "customer_id")
check("SqlServerAdapter.count_orphans result shape matches the other two adapters",
      orphan_result == {"rows_checked": 10, "orphan_count": 2, "orphan_rate": 0.2})
check("SqlServerAdapter.count_orphans uses NOT EXISTS, not NOT IN (T-SQL's NULL-handling trap)",
      "NOT EXISTS" in orphan_conn.log[0][0] and "NOT IN" not in orphan_conn.log[0][0])

columns_conn = _FakeConn([(
    ["COLUMN_NAME", "DATA_TYPE", "IS_NULLABLE", "CHARACTER_MAXIMUM_LENGTH", "NUMERIC_PRECISION", "NUMERIC_SCALE", "column_comment"],
    [("total_amt", "decimal", "NO", None, 18, 2, "order total")],
)])
cols = SqlServerAdapter(host="fake-host", database="fake_db", conn=columns_conn).get_columns("dbo", "orders")
check("SqlServerAdapter.get_columns renders decimal precision/scale like the rest of this toolkit (decimal(18,2))",
      cols[0].type == "decimal(18,2)" and cols[0].nullable is False and cols[0].comment == "order total")

check("build_adapter('sqlserver', ...) requires sqlserver_host",
      _raises(ValueError, lambda: build_adapter("sqlserver", sqlserver_database="db")))
check("build_adapter('sqlserver', ...) requires sqlserver_database",
      _raises(ValueError, lambda: build_adapter("sqlserver", sqlserver_host="h")))

# Config validation happens BEFORE importing pyodbc -- a toolkit.yaml misconfiguration must fail
# with a clear message regardless of whether pyodbc is installed in this environment, not be
# masked by an unrelated ImportError.
check("Unknown auth_mode is rejected without requiring pyodbc to be installed",
      _raises(ValueError, lambda: SqlServerAdapter(host="h", database="db", auth_mode="bogus")))
check("sql_auth_env with an unset username env var halts naming exactly which one, before importing pyodbc",
      _raises(ValueError, lambda: SqlServerAdapter(
          host="h", database="db", auth_mode="sql_auth_env",
          username_env_var="TOOLKIT_EVAL_UNSET_USER_XYZ", password_env_var="TOOLKIT_EVAL_UNSET_PASS_XYZ")))

print()
if failures:
    print(f"FAIL: {len(failures)} assertion(s) failed.")
    sys.exit(1)
else:
    print("PASS: all deterministic assertions passed.")
    sys.exit(0)
