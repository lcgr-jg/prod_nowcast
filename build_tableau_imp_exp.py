"""
Build a Tableau-ready long master table (exports ∪ imports) from Kpler trades.

Each delivered trade×product becomes two rows with normalized location columns:
  - Export row: Country/Zone/Installation/... come from the origin side
  - Import row: the same columns come from the destination side

Adds:
  - "Imp/Exp": Export | Import
  - kb_imp_exp: Volume (bbl) / 1000, positive for exports, negative for imports

Default filters match the terminal catalog: Trade status = Delivered, exclude
crude/condensate (Dirty / Crude / Condensate / Crude/Co).

Usage:
    python build_tableau_imp_exp.py
    python build_tableau_imp_exp.py --from-raw-csv
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from kpler_store import DB_PATH, DATA_DIR, _read_csv_sql, year_csv_paths

OUT_DIR = DATA_DIR / "derived" / "tableau"
PARQUET_PATH = OUT_DIR / "trades_imp_exp_master.parquet"
CSV_PATH = OUT_DIR / "trades_imp_exp_master.csv"
META_PATH = OUT_DIR / "trades_imp_exp_master_meta.json"

# Final column order for the stacked master (teammate mapping + enrichments)
MASTER_COLUMNS = [
    "Imp/Exp",
    "kb_imp_exp",
    "Vessel",
    "Country",
    "Zone",
    "Installation",
    "Continent",
    "Subcontinent",
    "TradingRegion",
    "Date",
    "Origin",
    "Destination",
    "Volume (bbl)",
    "Cargo (tons)",
    "Family",
    "Group",
    "Product",
    "Grade",
    "Estimated products",
    "Estimated Products - Confidences",
    "Trade status",
    "Seller",
    "Buyer",
    "Vessel Owner",
    "Vessel Owner 2",
]


def connect(*, from_raw_csv: bool = False) -> duckdb.DuckDBPyConnection:
    if from_raw_csv:
        files = year_csv_paths()
        if not files:
            raise FileNotFoundError("No kpler_YYYY.csv files under Raw Data/Kpler")
        con = duckdb.connect(":memory:")
        parts = [f"SELECT * FROM {_read_csv_sql(p)}" for p in files]
        con.execute("CREATE VIEW trades AS " + " UNION ALL BY NAME ".join(parts))
        return con
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Missing {DB_PATH}. Run bootstrap_duckdb.py first.")
    return duckdb.connect(str(DB_PATH), read_only=True)


def crude_filter_sql() -> str:
    # Same exclusion as build_terminal_catalog.py --exclude-crude
    return """
      AND NOT (
        Family = 'Dirty'
        OR Product = 'Crude'
        OR Product = 'Condensate'
        OR "Group" = 'Crude/Co'
      )
    """


def master_sql(status: str) -> str:
    """UNION ALL of export (origin-normalized) and import (destination-normalized)."""
    crude = crude_filter_sql()
    # kb = barrels / 1000; sign encodes direction for Tableau netting
    vol_kb = 'TRY_CAST("Volume (bbl)" AS DOUBLE) / 1000.0'

    exports = f"""
    SELECT
      'Export' AS "Imp/Exp",
      ({vol_kb}) AS kb_imp_exp,
      Vessel,
      "Country (origin)" AS Country,
      "Zone Origin" AS Zone,
      "Installation origin" AS Installation,
      "Continent Origin" AS Continent,
      "Subcontinent (origin)" AS Subcontinent,
      "TradingRegion (origin)" AS TradingRegion,
      TRY_CAST("Date (origin)" AS TIMESTAMP) AS Date,
      Origin,
      Destination,
      TRY_CAST("Volume (bbl)" AS DOUBLE) AS "Volume (bbl)",
      TRY_CAST("Cargo (tons)" AS DOUBLE) AS "Cargo (tons)",
      Family,
      "Group",
      Product,
      Grade,
      "Estimated products",
      "Estimated Products - Confidences",
      "Trade status",
      "Seller (origin)" AS Seller,
      "Buyer (destination)" AS Buyer,
      "Vessel Owner",
      "Vessel Owner 2"
    FROM trades
    WHERE "Trade status" = '{status}'
      {crude}
    """

    imports = f"""
    SELECT
      'Import' AS "Imp/Exp",
      -({vol_kb}) AS kb_imp_exp,
      Vessel,
      "Country (destination)" AS Country,
      "Zone Destination" AS Zone,
      "Installation Destination" AS Installation,
      "Continent Destination" AS Continent,
      "Subcontinent (destination)" AS Subcontinent,
      "TradingRegion (destination)" AS TradingRegion,
      TRY_CAST("Date (destination)" AS TIMESTAMP) AS Date,
      Origin,
      Destination,
      TRY_CAST("Volume (bbl)" AS DOUBLE) AS "Volume (bbl)",
      TRY_CAST("Cargo (tons)" AS DOUBLE) AS "Cargo (tons)",
      Family,
      "Group",
      Product,
      Grade,
      "Estimated products",
      "Estimated Products - Confidences",
      "Trade status",
      "Seller (origin)" AS Seller,
      "Buyer (destination)" AS Buyer,
      "Vessel Owner",
      "Vessel Owner 2"
    FROM trades
    WHERE "Trade status" = '{status}'
      {crude}
    """

    # Quote every identifier so Imp/Exp, Volume (bbl), etc. stay stable
    cols = ", ".join('"' + c.replace('"', '""') + '"' for c in MASTER_COLUMNS)
    return f"""
    SELECT {cols}
    FROM (
      {exports}
      UNION ALL
      {imports}
    ) AS stacked
    """


def build(
    *,
    status: str = "Delivered",
    from_raw_csv: bool = False,
) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    con = connect(from_raw_csv=from_raw_csv)
    try:
        sql = master_sql(status)
        # Materialize once, then write both formats from the temp table
        con.execute(f"CREATE TEMP TABLE tableau_master AS {sql}")

        n = con.execute("SELECT COUNT(*) FROM tableau_master").fetchone()[0]
        n_exp = con.execute(
            """SELECT COUNT(*) FROM tableau_master WHERE "Imp/Exp" = 'Export'"""
        ).fetchone()[0]
        n_imp = con.execute(
            """SELECT COUNT(*) FROM tableau_master WHERE "Imp/Exp" = 'Import'"""
        ).fetchone()[0]
        kb_sum = con.execute("SELECT SUM(kb_imp_exp) FROM tableau_master").fetchone()[0]
        kb_exp = con.execute(
            """SELECT SUM(kb_imp_exp) FROM tableau_master WHERE "Imp/Exp" = 'Export'"""
        ).fetchone()[0]
        kb_imp = con.execute(
            """SELECT SUM(kb_imp_exp) FROM tableau_master WHERE "Imp/Exp" = 'Import'"""
        ).fetchone()[0]

        # COPY via DuckDB is faster / lower memory than pandas for wide extracts
        pq = str(PARQUET_PATH).replace("'", "''")
        csv = str(CSV_PATH).replace("'", "''")
        con.execute(f"COPY tableau_master TO '{pq}' (FORMAT PARQUET)")
        con.execute(f"COPY tableau_master TO '{csv}' (HEADER, DELIMITER ',')")

        meta = {
            "built_at": datetime.now(timezone.utc).isoformat(),
            "status_filter": status,
            "exclude_crude": True,
            "kb_imp_exp_unit": "thousand barrels (bbl/1000); +Export / -Import",
            "rows_total": int(n),
            "rows_export": int(n_exp),
            "rows_import": int(n_imp),
            "kb_imp_exp_sum_net": float(kb_sum) if kb_sum is not None else None,
            "kb_export_sum": float(kb_exp) if kb_exp is not None else None,
            "kb_import_sum": float(kb_imp) if kb_imp is not None else None,
            "columns": MASTER_COLUMNS,
            "paths": {
                "parquet": str(PARQUET_PATH),
                "csv": str(CSV_PATH),
            },
            "source": "raw_csv" if from_raw_csv else str(DB_PATH),
            "tableau_tips": [
                "Filter Country / Installation / Zone for the selected location.",
                "Use Imp/Exp to split imports vs exports.",
                "SUM(kb_imp_exp) = net export (kb); do not SUM Volume across both directions without Imp/Exp.",
            ],
        }
        META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return meta
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", default="Delivered")
    parser.add_argument(
        "--from-raw-csv",
        action="store_true",
        help="Read yearly Kpler CSVs instead of DuckDB (if the DB is locked).",
    )
    args = parser.parse_args()
    try:
        meta = build(status=args.status, from_raw_csv=args.from_raw_csv)
    except Exception as exc:
        msg = str(exc).lower()
        if not args.from_raw_csv and (
            "being used by another process" in msg or "cannot open file" in msg
        ):
            print("DuckDB locked; falling back to --from-raw-csv ...")
            meta = build(status=args.status, from_raw_csv=True)
        else:
            raise
    print(json.dumps(meta, indent=2))
    print("done")


if __name__ == "__main__":
    main()
