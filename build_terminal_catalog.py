"""
Build a global terminal catalog from the DuckDB trades store.

One row per installation (prefer Kpler installation id), with import/export
activity, product mix, partner countries, and empty enrichment columns for
later desk research (demand centers, pipeline links, regions covered).

Usage:
    python build_terminal_catalog.py
    python build_terminal_catalog.py --status Delivered --exclude-crude
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from kpler_store import DB_PATH, DATA_DIR, _read_csv_sql, year_csv_paths

OUT_DIR = DATA_DIR / "derived"
ROOT = Path(__file__).resolve().parent
TERMINALS_PATH = OUT_DIR / "terminals.parquet"
PRODUCTS_PATH = OUT_DIR / "terminal_products.parquet"
PARTNERS_PATH = OUT_DIR / "terminal_partners.parquet"
ENRICHMENT_TEMPLATE = OUT_DIR / "terminals_enrichment_template.csv"
# Full wide CSV (all terminal cols + product/partner summaries)
TERMINALS_CSV = OUT_DIR / "terminals.csv"
TERMINALS_FULL_CSV = OUT_DIR / "terminals_full.csv"
# Desk-review master: location attrs from trades + role/volume/top mix
TERMINALS_MASTER_CSV = OUT_DIR / "terminals_master.csv"
NOTEBOOKS_CAT_CSV = ROOT / "notebooks" / "terminal_cat.csv"
PRODUCTS_CSV = OUT_DIR / "terminal_products.csv"
PARTNERS_CSV = OUT_DIR / "terminal_partners.csv"

# Location/installation attributes kept on the terminal master (not trade/vessel fields)
LOCATION_COLS = [
    "installation_id",
    "installation_name",
    "country",
    "zone",
    "zone_id",
    "continent",
    "subcontinent",
    "trading_region",
    "location",
]


def connect(*, from_raw_csv: bool = False) -> duckdb.DuckDBPyConnection:
    """Open trades from DuckDB, or (fallback) a view over yearly Kpler CSVs."""
    if from_raw_csv:
        files = year_csv_paths()
        if not files:
            raise FileNotFoundError("No kpler_YYYY.csv files under Raw Data/Kpler")
        con = duckdb.connect(":memory:")
        # UNION ALL keeps schema stable across years without materializing ~1GB
        parts = [f"SELECT * FROM {_read_csv_sql(p)}" for p in files]
        con.execute("CREATE VIEW trades AS " + " UNION ALL BY NAME ".join(parts))
        return con
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Missing {DB_PATH}. Run bootstrap_duckdb.py first.")
    return duckdb.connect(str(DB_PATH), read_only=True)


def crude_filter(exclude_crude: bool) -> str:
    if not exclude_crude:
        return ""
    return """
      AND NOT (
        Family = 'Dirty'
        OR Product = 'Crude'
        OR Product = 'Condensate'
        OR "Group" = 'Crude/Co'
      )
    """


def build_catalog(
    *,
    status: str = "Delivered",
    exclude_crude: bool = False,
    from_raw_csv: bool = False,
) -> dict[str, pd.DataFrame]:
    con = connect(from_raw_csv=from_raw_csv)
    crude = crude_filter(exclude_crude)
    vol = 'TRY_CAST("Volume (bbl)" AS DOUBLE)'

    # Exports: cargo leaving the installation (location attrs from origin side)
    exports = con.execute(
        f"""
        SELECT
          CAST("Installation origin id" AS VARCHAR) AS installation_id,
          "Installation origin" AS installation_name,
          "Country (origin)" AS country,
          "Zone Origin" AS zone,
          CAST("Zone Origin Id" AS VARCHAR) AS zone_id,
          "Continent Origin" AS continent,
          "Subcontinent (origin)" AS subcontinent,
          "TradingRegion (origin)" AS trading_region,
          "Origin" AS location,
          SUM({vol}) AS export_bbl,
          COUNT(*) AS export_trades,
          MIN(TRY_CAST("Date (origin)" AS TIMESTAMP)) AS first_export,
          MAX(TRY_CAST("Date (origin)" AS TIMESTAMP)) AS last_export
        FROM trades
        WHERE "Trade status" = ?
          AND "Installation origin" IS NOT NULL
          AND TRIM("Installation origin") <> ''
          {crude}
        GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
        """,
        [status],
    ).fetchdf()

    # Imports: cargo arriving at the installation (location attrs from destination side)
    imports = con.execute(
        f"""
        SELECT
          CAST("Installation Destination Id" AS VARCHAR) AS installation_id,
          "Installation Destination" AS installation_name,
          "Country (destination)" AS country,
          "Zone Destination" AS zone,
          CAST("Zone Destination Id" AS VARCHAR) AS zone_id,
          "Continent Destination" AS continent,
          "Subcontinent (destination)" AS subcontinent,
          "TradingRegion (destination)" AS trading_region,
          "Destination" AS location,
          SUM({vol}) AS import_bbl,
          COUNT(*) AS import_trades,
          MIN(TRY_CAST("Date (destination)" AS TIMESTAMP)) AS first_import,
          MAX(TRY_CAST("Date (destination)" AS TIMESTAMP)) AS last_import
        FROM trades
        WHERE "Trade status" = ?
          AND "Installation Destination" IS NOT NULL
          AND TRIM("Installation Destination") <> ''
          {crude}
        GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
        """,
        [status],
    ).fetchdf()

    # Prefer id; fall back to name key when id is null/blank
    def key_frame(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["installation_id"] = out["installation_id"].replace({"": pd.NA, "nan": pd.NA})
        out["terminal_key"] = out["installation_id"].fillna(
            "name:" + out["installation_name"].astype(str)
        )
        return out

    exports = key_frame(exports)
    imports = key_frame(imports)

    # Collapse rare duplicate geo contexts for same terminal key (keep max-volume row's geo)
    geo_cols = ["terminal_key", *LOCATION_COLS]
    exp_geo = (
        exports.sort_values("export_bbl", ascending=False)
        .groupby("terminal_key", as_index=False)
        .first()[geo_cols]
    )
    exp_agg = exports.groupby("terminal_key", as_index=False).agg(
        export_bbl=("export_bbl", "sum"),
        export_trades=("export_trades", "sum"),
        first_export=("first_export", "min"),
        last_export=("last_export", "max"),
    )
    exp = exp_geo.merge(exp_agg, on="terminal_key")

    imp_geo = (
        imports.sort_values("import_bbl", ascending=False)
        .groupby("terminal_key", as_index=False)
        .first()[geo_cols]
    )
    imp_agg = imports.groupby("terminal_key", as_index=False).agg(
        import_bbl=("import_bbl", "sum"),
        import_trades=("import_trades", "sum"),
        first_import=("first_import", "min"),
        last_import=("last_import", "max"),
    )
    imp = imp_geo.merge(imp_agg, on="terminal_key")

    terminals = exp.merge(
        imp,
        on="terminal_key",
        how="outer",
        suffixes=("_exp", "_imp"),
    )

    def coalesce(a, b):
        return a.where(a.notna() & (a.astype(str) != "nan"), b)

    for col in LOCATION_COLS:
        terminals[col] = coalesce(terminals[f"{col}_exp"], terminals[f"{col}_imp"])

    for c in [
        "export_bbl",
        "import_bbl",
        "export_trades",
        "import_trades",
    ]:
        if c not in terminals.columns:
            terminals[c] = 0.0
        terminals[c] = terminals[c].fillna(0.0)

    terminals["export_mb"] = terminals["export_bbl"] / 1e6
    terminals["import_mb"] = terminals["import_bbl"] / 1e6
    terminals["total_mb"] = terminals["export_mb"] + terminals["import_mb"]
    terminals["net_export_mb"] = terminals["export_mb"] - terminals["import_mb"]

    def role(row) -> str:
        e, i = row["export_mb"], row["import_mb"]
        if e > 0 and i > 0:
            return "hub"
        if e > 0:
            return "export_only"
        if i > 0:
            return "import_only"
        return "unknown"

    terminals["role"] = terminals.apply(role, axis=1)
    terminals["looks_like_refinery"] = (
        terminals["installation_name"].fillna("").str.contains("refinery", case=False)
    )

    # Activity window
    terminals["first_seen"] = terminals[["first_export", "first_import"]].min(axis=1)
    terminals["last_seen"] = terminals[["last_export", "last_import"]].max(axis=1)

    # Enrichment placeholders for desk research / later joins
    terminals["demand_centers"] = pd.NA  # free text / JSON later
    terminals["regions_covered"] = pd.NA
    terminals["pipeline_connected"] = pd.NA  # True/False/Unknown
    terminals["pipeline_names"] = pd.NA
    terminals["lat"] = pd.NA
    terminals["lon"] = pd.NA
    terminals["notes"] = pd.NA
    terminals["catalog_built_at"] = datetime.now(timezone.utc).isoformat()
    terminals["status_filter"] = status
    terminals["exclude_crude"] = exclude_crude

    keep = [
        "terminal_key",
        *LOCATION_COLS,
        "role",
        "looks_like_refinery",
        "export_mb",
        "import_mb",
        "total_mb",
        "net_export_mb",
        "export_trades",
        "import_trades",
        "first_seen",
        "last_seen",
        "demand_centers",
        "regions_covered",
        "pipeline_connected",
        "pipeline_names",
        "lat",
        "lon",
        "notes",
        "catalog_built_at",
        "status_filter",
        "exclude_crude",
    ]
    # LOCATION_COLS already includes installation_*; drop accidental duplicates
    keep = list(dict.fromkeys(keep))
    terminals = terminals[keep].sort_values("total_mb", ascending=False).reset_index(drop=True)

    # --- Product mix per terminal ---
    products_exp = con.execute(
        f"""
        SELECT
          CAST("Installation origin id" AS VARCHAR) AS installation_id,
          "Installation origin" AS installation_name,
          COALESCE(Family, '(unknown)') AS family,
          COALESCE(Product, '(blank)') AS product,
          SUM({vol}) AS bbl,
          'export' AS direction
        FROM trades
        WHERE "Trade status" = ?
          AND "Installation origin" IS NOT NULL
          AND TRIM("Installation origin") <> ''
          {crude}
        GROUP BY 1, 2, 3, 4
        """,
        [status],
    ).fetchdf()
    products_imp = con.execute(
        f"""
        SELECT
          CAST("Installation Destination Id" AS VARCHAR) AS installation_id,
          "Installation Destination" AS installation_name,
          COALESCE(Family, '(unknown)') AS family,
          COALESCE(Product, '(blank)') AS product,
          SUM({vol}) AS bbl,
          'import' AS direction
        FROM trades
        WHERE "Trade status" = ?
          AND "Installation Destination" IS NOT NULL
          AND TRIM("Installation Destination") <> ''
          {crude}
        GROUP BY 1, 2, 3, 4
        """,
        [status],
    ).fetchdf()
    products = pd.concat([products_exp, products_imp], ignore_index=True)
    products = key_frame(products)
    products["mb"] = products["bbl"] / 1e6
    products = products[
        ["terminal_key", "installation_id", "installation_name", "direction", "family", "product", "mb"]
    ].sort_values(["terminal_key", "direction", "mb"], ascending=[True, True, False])

    # --- Top partner countries per terminal ---
    partners_exp = con.execute(
        f"""
        SELECT
          CAST("Installation origin id" AS VARCHAR) AS installation_id,
          "Installation origin" AS installation_name,
          COALESCE("Country (destination)", '(unknown)') AS partner_country,
          SUM({vol}) AS bbl,
          'export_to' AS direction
        FROM trades
        WHERE "Trade status" = ?
          AND "Installation origin" IS NOT NULL
          AND TRIM("Installation origin") <> ''
          {crude}
        GROUP BY 1, 2, 3
        """,
        [status],
    ).fetchdf()
    partners_imp = con.execute(
        f"""
        SELECT
          CAST("Installation Destination Id" AS VARCHAR) AS installation_id,
          "Installation Destination" AS installation_name,
          COALESCE("Country (origin)", '(unknown)') AS partner_country,
          SUM({vol}) AS bbl,
          'import_from' AS direction
        FROM trades
        WHERE "Trade status" = ?
          AND "Installation Destination" IS NOT NULL
          AND TRIM("Installation Destination") <> ''
          {crude}
        GROUP BY 1, 2, 3
        """,
        [status],
    ).fetchdf()
    partners = pd.concat([partners_exp, partners_imp], ignore_index=True)
    partners = key_frame(partners)
    partners["mb"] = partners["bbl"] / 1e6
    # Keep top 15 partners per terminal × direction
    partners = (
        partners.sort_values("mb", ascending=False)
        .groupby(["terminal_key", "direction"], as_index=False, group_keys=False)
        .head(15)
    )
    partners = partners[
        [
            "terminal_key",
            "installation_id",
            "installation_name",
            "direction",
            "partner_country",
            "mb",
        ]
    ]

    con.close()
    return {
        "terminals": terminals,
        "products": products,
        "partners": partners,
    }


def _top_labels(df: pd.DataFrame, key: str, label_col: str, n: int = 5) -> pd.Series:
    """Semicolon-joined 'label (mb)' for top-n rows per terminal_key."""
    if df.empty:
        return pd.Series(dtype=str)

    def fmt(g: pd.DataFrame) -> str:
        rows = g.nlargest(n, "mb")
        return "; ".join(
            f"{r[label_col]} ({r['mb']:.1f})" for _, r in rows.iterrows()
        )

    return df.groupby(key, sort=False).apply(fmt, include_groups=False)


def build_full_terminals_csv(
    terminals: pd.DataFrame, products: pd.DataFrame, partners: pd.DataFrame
) -> pd.DataFrame:
    """All terminal columns plus top product/partner summaries."""
    out = terminals.copy()

    exp_prod = products[products["direction"] == "export"]
    imp_prod = products[products["direction"] == "import"]
    exp_part = partners[partners["direction"] == "export_to"]
    imp_part = partners[partners["direction"] == "import_from"]

    out["top_export_products"] = out["terminal_key"].map(
        _top_labels(exp_prod, "terminal_key", "product")
    )
    out["top_import_products"] = out["terminal_key"].map(
        _top_labels(imp_prod, "terminal_key", "product")
    )
    out["top_export_to_countries"] = out["terminal_key"].map(
        _top_labels(exp_part, "terminal_key", "partner_country")
    )
    out["top_import_from_countries"] = out["terminal_key"].map(
        _top_labels(imp_part, "terminal_key", "partner_country")
    )

    out["n_export_products"] = (
        out["terminal_key"].map(exp_prod.groupby("terminal_key").size()).fillna(0).astype(int)
    )
    out["n_import_products"] = (
        out["terminal_key"].map(imp_prod.groupby("terminal_key").size()).fillna(0).astype(int)
    )
    out["n_export_partners"] = (
        out["terminal_key"].map(exp_part.groupby("terminal_key").size()).fillna(0).astype(int)
    )
    out["n_import_partners"] = (
        out["terminal_key"].map(imp_part.groupby("terminal_key").size()).fillna(0).astype(int)
    )
    return out


def build_terminals_master_csv(full: pd.DataFrame) -> pd.DataFrame:
    """Desk-review CSV: location attrs + role/volumes/top mix (no empty enrichment cols)."""
    master_cols = [
        "terminal_key",
        *LOCATION_COLS,
        "role",
        "looks_like_refinery",
        "export_mb",
        "import_mb",
        "total_mb",
        "net_export_mb",
        "export_trades",
        "import_trades",
        "first_seen",
        "last_seen",
        "top_export_products",
        "top_import_products",
        "top_export_to_countries",
        "top_import_from_countries",
        "n_export_products",
        "n_import_products",
        "n_export_partners",
        "n_import_partners",
        "status_filter",
        "exclude_crude",
        "catalog_built_at",
    ]
    master_cols = list(dict.fromkeys(master_cols))
    return full[master_cols].copy()


def write_outputs(tables: dict[str, pd.DataFrame]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    NOTEBOOKS_CAT_CSV.parent.mkdir(parents=True, exist_ok=True)

    tables["terminals"].to_parquet(TERMINALS_PATH, index=False)
    tables["products"].to_parquet(PRODUCTS_PATH, index=False)
    tables["partners"].to_parquet(PARTNERS_PATH, index=False)

    # All terminal columns (no index) — same schema as the parquet master
    tables["terminals"].to_csv(TERMINALS_CSV, index=False)
    tables["products"].to_csv(PRODUCTS_CSV, index=False)
    tables["partners"].to_csv(PARTNERS_CSV, index=False)

    # Wide CSV: every terminal column + product/partner summaries
    full = build_full_terminals_csv(
        tables["terminals"], tables["products"], tables["partners"]
    )
    full.to_csv(TERMINALS_FULL_CSV, index=False)

    # Desk-review master first (location attrs + aggregates; no enrichment placeholders)
    master = build_terminals_master_csv(full)
    master.to_csv(TERMINALS_MASTER_CSV, index=False)

    # Keep notebooks/terminal_cat.csv as the desk-facing full export (may be open in Excel)
    try:
        full.to_csv(NOTEBOOKS_CAT_CSV, index=False)
    except PermissionError:
        print(f"skip (locked): {NOTEBOOKS_CAT_CSV}")

    # Lightweight template for manual enrichment (merge later on terminal_key)
    enrich_cols = [
        "terminal_key",
        "installation_id",
        "installation_name",
        "country",
        "demand_centers",
        "regions_covered",
        "pipeline_connected",
        "pipeline_names",
        "lat",
        "lon",
        "notes",
    ]
    tmpl = tables["terminals"][enrich_cols].copy()
    tmpl.to_csv(ENRICHMENT_TEMPLATE, index=False)

    meta = {
        "terminals": len(tables["terminals"]),
        "products_rows": len(tables["products"]),
        "partners_rows": len(tables["partners"]),
        "full_csv_columns": list(full.columns),
        "paths": {
            "terminals_parquet": str(TERMINALS_PATH),
            "terminals_csv": str(TERMINALS_CSV),
            "terminals_full_csv": str(TERMINALS_FULL_CSV),
            "terminals_master_csv": str(TERMINALS_MASTER_CSV),
            "notebooks_terminal_cat_csv": str(NOTEBOOKS_CAT_CSV),
            "products_csv": str(PRODUCTS_CSV),
            "partners_csv": str(PARTNERS_CSV),
            "enrichment_template": str(ENRICHMENT_TEMPLATE),
        },
    }
    (OUT_DIR / "terminal_catalog_meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    print(json.dumps(meta, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", default="Delivered")
    parser.add_argument(
        "--exclude-crude",
        action="store_true",
        help="Drop Dirty/Crude/Condensate/Crude-Co from the catalog volumes.",
    )
    parser.add_argument(
        "--from-raw-csv",
        action="store_true",
        help="Read yearly Kpler CSVs instead of kpler_trades.duckdb (e.g. if DB is locked).",
    )
    args = parser.parse_args()
    try:
        tables = build_catalog(
            status=args.status,
            exclude_crude=args.exclude_crude,
            from_raw_csv=args.from_raw_csv,
        )
    except Exception as exc:
        # Auto-fallback when DBeaver (or another process) holds an exclusive lock
        msg = str(exc).lower()
        if not args.from_raw_csv and (
            "being used by another process" in msg or "cannot open file" in msg
        ):
            print("DuckDB locked; falling back to --from-raw-csv ...")
            tables = build_catalog(
                status=args.status,
                exclude_crude=args.exclude_crude,
                from_raw_csv=True,
            )
        else:
            raise
    write_outputs(tables)
    t = tables["terminals"]
    print(
        f"Roles: hub={int((t.role=='hub').sum())}, "
        f"export_only={int((t.role=='export_only').sum())}, "
        f"import_only={int((t.role=='import_only').sum())}"
    )
    print("Top 10 by total_mb:")
    print(
        t.head(10)[
            ["installation_name", "country", "role", "export_mb", "import_mb", "total_mb"]
        ].to_string(index=False)
    )
    print("done")


if __name__ == "__main__":
    main()
