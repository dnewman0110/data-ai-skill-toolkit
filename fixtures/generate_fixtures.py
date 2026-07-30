#!/usr/bin/env python3
"""
generate_fixtures.py -- builds the synthetic fixture lakehouse every skill's evals run against.

100% synthetic, fixed-seed, deterministic. No client data has ever touched this file -- see
CONTRIBUTING.md. Produces three SQLite files under fixtures/lakehouse/ (bronze.db, silver.db,
gold.db), simulating a Databricks/Unity Catalog three-level catalog.schema.table hierarchy: the
"catalog" is the conceptual label "acme_retail_dev" (configured by whoever loads these fixtures,
not stored here), each SQLite file is a "schema", and tables within it are addressed the normal
way. See scripts/lakehouse_adapter.py for how skills query this uniformly whether they're
pointed at these fixture files or a real workspace.

Layout modeled:
  bronze.raw_orders       -- raw ingestion artifact: messy names, string types, a _rescued_data
                             column, ingest metadata, duplicate re-ingested rows. Exists so
                             data-modeling's silver-verification check has something to correctly
                             REJECT (gold-layer modeling must refuse to source from this).
  silver.customers        -- curated dimension source, WITH one deliberate flaw:
                             a duplicated natural key (customer_number 'CUST-1042' appears
                             twice, under two different surrogate customer_id values).
  silver.customer_region_history
                           -- effective-dated region history per customer: the "slowly changing
                             attribute" flaw/scenario data-modeling should recognize as SCD
                             type 2 material.
  silver.orders            -- curated fact-grain source, one row per order line, WITH three
                             deliberate flaws:
                               - a broken FK: one order references a customer_id that does not
                                 exist in silver.customers (orphan).
                               - a nullable column that shouldn't be: ship_region has a few NULLs
                                 despite being, in practice, always populated (no NOT NULL
                                 constraint declared, but profiling should show ~0% null rate
                                 except for these).
                               - a type mismatch between source and target: total_amt is stored
                                 as TEXT ("129.99") in silver, while the existing legacy gold
                                 table represents the equivalent measure as REAL.
  gold.legacy_fct_orders   -- a pre-existing (hand-built, not toolkit-generated) gold table,
                             standing in for "what's already in production" so data-validation
                             has a real source/target pair with genuine discrepancies to find:
                             missing rows (orphaned customer_id was filtered by an inner join),
                             and the total_amt type-mismatch surfacing as a value mismatch once
                             compared.

Column/table comments are stored in companion `_table_comments` / `_column_comments` tables
within each schema file (SQLite has no native COMMENT ON) so data-modeling's "documented column
comments" signal is genuinely checkable, not hand-waved.

Usage: python fixtures/generate_fixtures.py [--out-dir fixtures/lakehouse] [--seed 20260729]
"""
import argparse
import random
import sqlite3
from pathlib import Path

SEED = 20260729


def fresh_db(path: Path) -> sqlite3.Connection:
    # Prefer deleting and recreating; fall back to dropping all existing tables in place if the
    # filesystem doesn't allow unlink (some network/FUSE-mounted dev environments restrict
    # delete). journal_mode=MEMORY + synchronous=OFF also sidesteps journal-file write issues
    # some of those same filesystems have with SQLite's default rollback journal -- fine for a
    # disposable, regenerate-anytime fixture DB, not a choice you'd make for real data.
    if path.exists():
        try:
            path.unlink()
        except OSError:
            # Some restricted/network filesystems allow overwriting a file's contents but not
            # deleting it. Truncating in place gets us the same "fresh file" result.
            path.write_bytes(b"")
        journal = path.with_name(path.name + "-journal")
        if journal.exists():
            try:
                journal.unlink()
            except OSError:
                journal.write_bytes(b"")
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=MEMORY")
    conn.execute("PRAGMA synchronous=OFF")
    existing = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    for t in existing:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.execute("""
        CREATE TABLE _table_comments (table_name TEXT PRIMARY KEY, comment TEXT)
    """)
    conn.execute("""
        CREATE TABLE _column_comments (table_name TEXT, column_name TEXT, comment TEXT,
                                        PRIMARY KEY (table_name, column_name))
    """)
    return conn


def set_table_comment(conn, table, comment):
    conn.execute("INSERT INTO _table_comments VALUES (?, ?)", (table, comment))


def set_column_comment(conn, table, column, comment):
    conn.execute("INSERT INTO _column_comments VALUES (?, ?, ?)", (table, column, comment))


def build_bronze(out_dir: Path, rng: random.Random):
    conn = fresh_db(out_dir / "bronze.db")
    conn.execute("""
        CREATE TABLE raw_orders (
            OrderID TEXT,
            CustID TEXT,
            TotalAmt TEXT,
            _rescued_data TEXT,
            _ingest_file_name TEXT,
            _ingest_ts TEXT
        )
    """)
    # No table/column comments, messy PascalCase source-system naming, string-typed everything,
    # duplicate rows from re-ingestion (the same OrderID appears from two different ingest files)
    # -- this is what an uncurated bronze/raw layer actually looks like, and is exactly what
    # data-modeling's silver_verification check must detect and refuse.
    rows = []
    for i in range(1, 51):
        rows.append((str(1000 + i), str(500 + (i % 40)), f"{rng.uniform(10, 500):.2f}", None,
                     "orders_2026_07_28.csv", "2026-07-28T02:00:00Z"))
    # duplicate re-ingestion of the first 5 orders under a second file
    for i in range(1, 6):
        rows.append((str(1000 + i), str(500 + (i % 40)), f"{rng.uniform(10, 500):.2f}", None,
                     "orders_2026_07_28_retry.csv", "2026-07-28T04:00:00Z"))
    conn.executemany("INSERT INTO raw_orders VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def build_silver(out_dir: Path, rng: random.Random):
    conn = fresh_db(out_dir / "silver.db")

    conn.execute("""
        CREATE TABLE customers (
            customer_id INTEGER PRIMARY KEY,
            customer_number TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT,
            region TEXT,
            signup_date TEXT NOT NULL
        )
    """)
    set_table_comment(conn, "customers",
                       "Curated customer dimension source. One row per customer_id (surrogate key). "
                       "customer_number is the upstream CRM's business key and is expected to be unique "
                       "per real-world customer.")
    set_column_comment(conn, "customers", "customer_id", "Surrogate key, generated at curation time.")
    set_column_comment(conn, "customers", "customer_number",
                        "Business key from source CRM system (format CUST-NNNN).")
    set_column_comment(conn, "customers", "region", "Customer's current sales region. See "
                        "customer_region_history for how this has changed over time.")

    regions = ["northeast", "midwest", "south", "west"]
    customer_rows = []
    for cid in range(1, 121):
        customer_rows.append((
            cid, f"CUST-{1000 + cid}", f"Customer {cid}",
            f"customer{cid}@example.com" if cid % 7 else None,
            regions[cid % len(regions)],
            f"2024-{(cid % 12) + 1:02d}-{(cid % 28) + 1:02d}",
        ))
    # FLAW 2: duplicated natural key. customer_id 121 and 122 both carry customer_number
    # 'CUST-1042' (the same business key as customer_id 42) -- a dedup failure upstream.
    customer_rows.append((121, "CUST-1042", "Customer 42 (dup A)", "dup.a@example.com", "northeast", "2025-01-01"))
    customer_rows.append((122, "CUST-1042", "Customer 42 (dup B)", "dup.b@example.com", "northeast", "2025-06-15"))
    conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?)", customer_rows)

    conn.execute("""
        CREATE TABLE customer_region_history (
            customer_id INTEGER NOT NULL,
            region TEXT NOT NULL,
            effective_date TEXT NOT NULL,
            end_date TEXT
        )
    """)
    set_table_comment(conn, "customer_region_history",
                       "Effective-dated history of customer region reassignment. Present because sales "
                       "commission recalculation requires attributing historical orders to the region a "
                       "customer was in AT THE TIME of the order, not their current region.")
    history_rows = [
        (7, "midwest", "2024-01-01", "2025-03-14"),
        (7, "west", "2025-03-15", None),
        (15, "south", "2024-06-01", "2026-01-09"),
        (15, "northeast", "2026-01-10", None),
    ]
    conn.executemany("INSERT INTO customer_region_history VALUES (?, ?, ?, ?)", history_rows)

    conn.execute("""
        CREATE TABLE orders (
            order_id INTEGER NOT NULL,
            line_number INTEGER NOT NULL,
            customer_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            total_amt TEXT NOT NULL,
            ship_region TEXT,
            order_date TEXT NOT NULL,
            PRIMARY KEY (order_id, line_number)
        )
    """)
    set_table_comment(conn, "orders",
                       "Curated order line fact source. One row per order line item "
                       "(order_id, line_number).")
    set_column_comment(conn, "orders", "total_amt",
                        "Line total in USD. NOTE: stored as text pending a source-system fix; downstream "
                        "consumers must cast.")
    set_column_comment(conn, "orders", "ship_region", "Shipping region at time of order.")

    order_rows = []
    order_id = 5000
    for cid in list(range(1, 121)):
        for line in range(1, rng.randint(1, 3) + 1):
            order_id += 1
            ship_region = regions[(order_id + line) % len(regions)]
            # FLAW 3: nullable column that shouldn't be -- a handful of orders (every 37th) have
            # a NULL ship_region despite the field being populated in practice everywhere else.
            if order_id % 37 == 0:
                ship_region = None
            order_rows.append((
                order_id, line, cid, 100 + (order_id % 25), rng.randint(1, 5),
                f"{rng.uniform(9.99, 499.99):.2f}", ship_region,
                f"2026-0{(order_id % 6) + 1}-{(order_id % 27) + 1:02d}",
            ))
    # FLAW 1: broken FK -- one order references a customer_id that does not exist in
    # silver.customers at all (99999).
    order_id += 1
    order_rows.append((order_id, 1, 99999, 101, 1, "42.00", "west", "2026-07-15"))
    conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)", order_rows)

    conn.commit()
    conn.close()
    return order_rows


def build_gold(out_dir: Path, silver_order_rows, rng: random.Random):
    conn = fresh_db(out_dir / "gold.db")
    conn.execute("""
        CREATE TABLE legacy_fct_orders (
            order_id INTEGER NOT NULL,
            line_number INTEGER NOT NULL,
            customer_id INTEGER NOT NULL,
            total_amt REAL NOT NULL,
            PRIMARY KEY (order_id, line_number)
        )
    """)
    set_table_comment(conn, "legacy_fct_orders",
                       "Pre-existing production gold table (not built by this toolkit), standing in as "
                       "a validation TARGET to compare against silver.orders as SOURCE. Built by an "
                       "inner join to customers, so orders with no matching customer are silently "
                       "dropped -- this is the discrepancy data-validation is expected to find.")
    rows = []
    for r in silver_order_rows:
        order_id, line_number, customer_id, product_id, quantity, total_amt, ship_region, order_date = r
        if customer_id == 99999:
            continue  # FLAW 4 surfacing: inner join drops the orphaned-customer order entirely
        rows.append((order_id, line_number, customer_id, float(total_amt)))  # type coercion happens here
    conn.executemany("INSERT INTO legacy_fct_orders VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=str(Path(__file__).resolve().parent / "lakehouse"))
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)

    build_bronze(out_dir, rng)
    silver_order_rows = build_silver(out_dir, rng)
    build_gold(out_dir, silver_order_rows, rng)

    print(f"Fixture lakehouse written to {out_dir}: bronze.db, silver.db, gold.db")
    print("Deliberate flaws: broken FK (silver.orders customer_id=99999), duplicated natural key "
          "(silver.customers customer_number='CUST-1042'), nullable-that-shouldn't-be "
          "(silver.orders.ship_region), type mismatch (silver.orders.total_amt TEXT vs "
          "gold.legacy_fct_orders.total_amt REAL), slowly changing attribute "
          "(silver.customer_region_history).")


if __name__ == "__main__":
    main()
