"""
DuckDB canonical store for Kpler Liquids trades.

Upsert key: (Id (Trade), Id (Product)) — one trade can split across products.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "Raw Data" / "Kpler"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "kpler_trades.duckdb"

TRADE_ID_COL = "Id (Trade)"
PRODUCT_ID_COL = "Id (Product)"
META_COLS = ("_source_file", "_ingested_at", "_updated_at")


def connect(db_path: Path | None = None, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path = db_path or DB_PATH
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path), read_only=read_only)


def year_csv_paths(raw_dir: Path | None = None) -> list[Path]:
    folder = raw_dir or RAW_DIR
    return sorted(folder.glob("kpler_20*.csv"))


def _q(col: str) -> str:
    return '"' + col.replace('"', '""') + '"'


def _read_csv_sql(path: Path) -> str:
    # Paths embedded as SQL string literals (files are local & trusted).
    # all_varchar avoids brittle type sniffing when a rare bad line shifts fields;
    # analytics cast Volume etc. at query time / via views.
    p = str(path).replace("'", "''")
    return f"""
        read_csv(
            '{p}',
            delim=';',
            header=true,
            quote='"',
            escape='"',
            all_varchar=true,
            ignore_errors=true,
            parallel=true,
            sample_size=-1
        )
    """


def bootstrap_from_yearly_csvs(
    *,
    db_path: Path | None = None,
    raw_dir: Path | None = None,
    replace: bool = True,
) -> dict[str, int]:
    """Load all kpler_YYYY.csv files into trades (full rebuild by default)."""
    files = year_csv_paths(raw_dir)
    if not files:
        raise FileNotFoundError(f"No kpler_YYYY.csv files under {raw_dir or RAW_DIR}")

    con = connect(db_path)
    try:
        if replace:
            con.execute("DROP TABLE IF EXISTS trades")

        now = datetime.now(timezone.utc).isoformat().replace("'", "''")
        first = files[0]
        print(f"  creating from {first.name} ...")
        con.execute(
            f"""
            CREATE TABLE trades AS
            SELECT
                *,
                '{first.name}' AS _source_file,
                '{now}' AS _ingested_at,
                '{now}' AS _updated_at
            FROM {_read_csv_sql(first)}
            """
        )

        for path in files[1:]:
            print(f"  inserting {path.name} ...")
            con.execute(
                f"""
                INSERT INTO trades BY NAME
                SELECT
                    *,
                    '{path.name}' AS _source_file,
                    '{now}' AS _ingested_at,
                    '{now}' AS _updated_at
                FROM {_read_csv_sql(path)}
                """
            )

        # PRIMARY KEY required for ON CONFLICT upserts on refresh.
        con.execute(
            f"""
            ALTER TABLE trades
            ADD PRIMARY KEY ({_q(TRADE_ID_COL)}, {_q(PRODUCT_ID_COL)})
            """
        )
        n = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        n_trades = con.execute(
            f"SELECT COUNT(DISTINCT {_q(TRADE_ID_COL)}) FROM trades"
        ).fetchone()[0]
        return {"rows": int(n), "distinct_trades": int(n_trades), "files": len(files)}
    finally:
        con.close()


def upsert_csv(
    csv_path: Path,
    *,
    db_path: Path | None = None,
    source_label: str | None = None,
) -> dict[str, int]:
    """
    Upsert rows from a Kpler trades CSV into trades.

    Requires an existing trades table (run bootstrap first).
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    con = connect(db_path)
    try:
        exists = con.execute(
            """
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_name = 'trades'
            """
        ).fetchone()[0]
        if not exists:
            raise RuntimeError("trades table missing — run bootstrap_duckdb.py first")

        now = datetime.now(timezone.utc).isoformat().replace("'", "''")
        label = (source_label or csv_path.name).replace("'", "''")
        con.execute("DROP TABLE IF EXISTS _staging_trades")
        con.execute(
            f"""
            CREATE TEMP TABLE _staging_trades AS
            SELECT
                *,
                '{label}' AS _source_file,
                '{now}' AS _ingested_at,
                '{now}' AS _updated_at
            FROM {_read_csv_sql(csv_path)}
            """
        )

        staged = con.execute("SELECT COUNT(*) FROM _staging_trades").fetchone()[0]

        # Columns present in both staging and target (order from staging).
        stage_cols = [
            r[0]
            for r in con.execute("DESCRIBE _staging_trades").fetchall()
        ]
        target_cols = {r[0] for r in con.execute("DESCRIBE trades").fetchall()}
        shared = [c for c in stage_cols if c in target_cols]
        if TRADE_ID_COL not in shared or PRODUCT_ID_COL not in shared:
            raise RuntimeError("Staging CSV missing trade/product id columns")

        insert_cols = ", ".join(_q(c) for c in shared)
        select_cols = ", ".join(f"s.{_q(c)}" for c in shared)
        update_assignments = ", ".join(
            f"{_q(c)} = excluded.{_q(c)}"
            for c in shared
            if c not in (TRADE_ID_COL, PRODUCT_ID_COL, "_ingested_at")
        )

        # Preserve original ingested_at on updates.
        con.execute(
            f"""
            INSERT INTO trades ({insert_cols})
            SELECT {select_cols}
            FROM _staging_trades s
            ON CONFLICT ({_q(TRADE_ID_COL)}, {_q(PRODUCT_ID_COL)})
            DO UPDATE SET {update_assignments}
            """
        )

        total = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        return {"staged_rows": int(staged), "table_rows": int(total)}
    finally:
        con.close()


def table_stats(db_path: Path | None = None) -> dict:
    con = connect(db_path, read_only=DB_PATH.exists() if db_path is None else True)
    try:
        rows = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        trades = con.execute(
            f'SELECT COUNT(DISTINCT {_q(TRADE_ID_COL)}) FROM trades'
        ).fetchone()[0]
        date_min, date_max = con.execute(
            'SELECT MIN("Date (origin)"), MAX("Date (origin)") FROM trades'
        ).fetchone()
        return {
            "rows": int(rows),
            "distinct_trades": int(trades),
            "date_origin_min": date_min,
            "date_origin_max": date_max,
            "db_path": str(db_path or DB_PATH),
        }
    finally:
        con.close()


def sanitize_country_filename(country: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", country).strip("_").lower()
