"""Crude export corridor EDA helpers on ``data/kpler_trades.duckdb``.

Toggle corridors via ``CORRIDORS[name]`` filters. Volumes in DuckDB may be
VARCHAR — always ``try_cast`` to DOUBLE.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

BBLS_PER_DAY_TO_MBD = 1e6  # volume_bbl / days / 1e6 → mbd

# Country-level flow scope (apply after load).
# INTER = true cross-border; INTRA = same origin/dest country (cabotage / domestic).
FLOW_SCOPES = ("ALL", "INTER", "INTRA")


@dataclass(frozen=True)
class Corridor:
    """Origin filter for crude export cuts."""

    key: str
    label: str
    # SQL fragment (no leading AND) using trades column names in quotes
    where_sql: str
    notes: str = ""


CORRIDORS: dict[str, Corridor] = {
    "BRAZIL": Corridor(
        key="BRAZIL",
        label="Brazil",
        where_sql="\"Country (origin)\" = 'Brazil'",
        notes="FPSO/FSO-heavy Latin America crude exports.",
    ),
    "WAF": Corridor(
        key="WAF",
        label="West Africa",
        where_sql="\"TradingRegion (origin)\" = 'Western Africa'",
        notes="Kpler trading region = Western Africa.",
    ),
    "MEG": Corridor(
        key="MEG",
        label="Mideast Gulf",
        where_sql="\"TradingRegion (origin)\" = 'Mideast Gulf'",
        notes="Excludes Other Middle East (e.g. Yanbu, Fujairah) by design.",
    ),
    "USG": Corridor(
        key="USG",
        label="US Gulf Coast",
        where_sql="\"TradingRegion (origin)\" = 'USGC (US Gulf Coast)'",
        notes="Kpler trading region = USGC (US Gulf Coast).",
    ),
}


SELECT_COLS = [
    '"Id (Trade)" AS trade_id',
    '"Id (Voyage)" AS voyage_id',
    '"Date (origin)" AS date_origin',
    '"Date (destination)" AS date_dest',
    '"Trade status" AS trade_status',
    '"Country (origin)" AS country_origin',
    '"TradingRegion (origin)" AS region_origin',
    '"Zone Origin" AS zone_origin',
    '"Installation origin" AS installation_origin',
    '"Origin" AS origin',
    '"Country (destination)" AS country_dest',
    '"TradingRegion (destination)" AS region_dest',
    '"Zone Destination" AS zone_dest',
    '"Destination" AS destination',
    '"Installation Destination" AS installation_dest',
    "Product AS product",
    '"Group" AS product_group',
    "Family AS family",
    "Grade AS grade",
    'try_cast("Volume (bbl)" AS DOUBLE) AS volume_bbl',
    'try_cast("Cargo (tons)" AS DOUBLE) AS cargo_tons',
    "Vessel AS vessel",
    '"IMO (vessel)" AS imo',
    '"Id (vessel)" AS vessel_id',
    '"Vessel type" AS vessel_type',
    'try_cast("Vessel DWT" AS DOUBLE) AS vessel_dwt',
    '"Vessel Owner" AS vessel_owner',
    # STS chain: vessel 1 ≈ mother/shuttle; vessel 2 ≈ daughter to final dest
    '"Vessel Name 2" AS vessel_2',
    '"Vessel IMO 2" AS imo_2',
    'try_cast("Vessel DWT 2" AS DOUBLE) AS vessel_dwt_2',
    '"Vessel Type 2" AS vessel_type_2',
    '"Vessel Owner 2" AS vessel_owner_2',
    '"Vessel Name 3" AS vessel_3',
    '"Vessel IMO 3" AS imo_3',
    'try_cast("Vessel DWT 3" AS DOUBLE) AS vessel_dwt_3',
    'try_cast("Mileage" AS DOUBLE) AS mileage',
    'try_cast("Ton Miles" AS DOUBLE) AS ton_miles',
    '"Zone STS 1" AS zone_sts_1',
    '"Installation STS 1" AS installation_sts_1',
    'try_cast("Volume STS 1" AS DOUBLE) AS volume_sts_1',
]


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path), read_only=True)


def load_crude_exports(
    con: duckdb.DuckDBPyConnection,
    corridor: Corridor | str,
    *,
    status: str = "Delivered",
    product: str = "Crude",
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """Load crude export trades for a corridor (origin filter)."""
    c = CORRIDORS[corridor] if isinstance(corridor, str) else corridor
    where = [
        c.where_sql,
        "Product = ?",
        '"Trade status" = ?',
    ]
    params: list[Any] = [product, status]
    if start:
        where.append('try_cast("Date (origin)" AS DATE) >= ?')
        params.append(start)
    if end:
        where.append('try_cast("Date (origin)" AS DATE) <= ?')
        params.append(end)
    sql = f"""
    SELECT {", ".join(SELECT_COLS)}
    FROM trades
    WHERE {" AND ".join(where)}
    """
    df = con.execute(sql, params).df()
    if df.empty:
        return df
    df["date_origin"] = pd.to_datetime(df["date_origin"], errors="coerce")
    df["date_dest"] = pd.to_datetime(df["date_dest"], errors="coerce")
    df["volume_bbl"] = pd.to_numeric(df["volume_bbl"], errors="coerce")
    df["vessel_dwt"] = pd.to_numeric(df["vessel_dwt"], errors="coerce")
    df["vessel_dwt_2"] = pd.to_numeric(df.get("vessel_dwt_2"), errors="coerce")
    df["vessel_dwt_3"] = pd.to_numeric(df.get("vessel_dwt_3"), errors="coerce")
    df["imo"] = pd.to_numeric(df["imo"], errors="coerce").astype("Int64")
    df["imo_2"] = pd.to_numeric(df.get("imo_2"), errors="coerce").astype("Int64")
    df["imo_3"] = pd.to_numeric(df.get("imo_3"), errors="coerce").astype("Int64")
    df["month"] = df["date_origin"].dt.to_period("M").dt.to_timestamp()
    df["year"] = df["date_origin"].dt.year
    df["has_sts"] = df["zone_sts_1"].notna() & (df["zone_sts_1"].astype(str).str.strip() != "")
    df["has_daughter"] = df["imo_2"].notna()
    df["dwt_bucket"] = df["vessel_dwt"].map(dwt_bucket)
    df["dwt_bucket_2"] = df["vessel_dwt_2"].map(dwt_bucket)
    df["corridor"] = c.key
    df = annotate_flow_scope(df)
    return df


def annotate_flow_scope(df: pd.DataFrame) -> pd.DataFrame:
    """Tag each row INTRA / INTER / UNKNOWN from origin vs dest country."""
    out = df.copy()
    o = out["country_origin"].astype("string").str.strip()
    d = out["country_dest"].astype("string").str.strip()
    known = o.notna() & d.notna() & (o != "") & (d != "") & (o != "<NA>") & (d != "<NA>")
    out["flow_scope"] = "UNKNOWN"
    out.loc[known & (o == d), "flow_scope"] = "INTRA"
    out.loc[known & (o != d), "flow_scope"] = "INTER"
    # Same trading-region flag (LatAm→Chile is INTER but regional)
    ro = out["region_origin"].astype("string").str.strip()
    rd = out["region_dest"].astype("string").str.strip()
    rk = ro.notna() & rd.notna() & (ro != "") & (rd != "")
    out["same_trading_region"] = False
    out.loc[rk, "same_trading_region"] = (ro.loc[rk] == rd.loc[rk]).to_numpy()
    return out


def apply_flow_scope(df: pd.DataFrame, scope: str = "INTER") -> pd.DataFrame:
    """Filter to ALL | INTER | INTRA. Default INTER = true exports."""
    scope = (scope or "ALL").upper()
    if scope not in FLOW_SCOPES:
        raise ValueError(f"FLOW_SCOPE must be one of {FLOW_SCOPES}, got {scope!r}")
    if scope == "ALL":
        return df.copy()
    return df.loc[df["flow_scope"] == scope].copy()


def flow_scope_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Cargo/volume split by INTRA vs INTER (and UNKNOWN)."""
    if df.empty:
        return pd.DataFrame()
    g = (
        df.groupby("flow_scope", as_index=False)
        .agg(cargoes=("trade_id", "nunique"), volume_bbl=("volume_bbl", "sum"))
    )
    g["share_cargoes"] = g["cargoes"] / g["cargoes"].sum()
    g["share_vol"] = g["volume_bbl"] / g["volume_bbl"].sum()
    return g.sort_values("volume_bbl", ascending=False)


def scope_metrics(df: pd.DataFrame) -> pd.Series:
    """Headline metrics for one filtered slice (used in INTRA vs INTER compare)."""
    if df.empty:
        return pd.Series(
            {
                "trades": 0,
                "unique_imo": 0,
                "volume_bbl": 0.0,
                "mbd_mean": np.nan,
                "china_vol_share": np.nan,
                "vlcc_plus_vol_share": np.nan,
                "suezmax_vol_share": np.nan,
                "top10_imo_lift_share": np.nan,
                "one_off_imo_share": np.nan,
                "sts_vol_share": np.nan,
            }
        )
    vol = float(df["volume_bbl"].sum())
    conc = fleet_concentration(df)
    m = monthly_export_mbd(df)
    return pd.Series(
        {
            "trades": int(df["trade_id"].nunique()),
            "unique_imo": int(df["imo"].nunique()),
            "volume_bbl": vol,
            "mbd_mean": float(m["mbd"].mean()) if len(m) else np.nan,
            "china_vol_share": float(df.loc[df["country_dest"] == "China", "volume_bbl"].sum() / vol)
            if vol
            else np.nan,
            "vlcc_plus_vol_share": float(df.loc[df["dwt_bucket"] == "VLCC+", "volume_bbl"].sum() / vol)
            if vol
            else np.nan,
            "suezmax_vol_share": float(df.loc[df["dwt_bucket"] == "Suezmax", "volume_bbl"].sum() / vol)
            if vol
            else np.nan,
            "top10_imo_lift_share": conc.get("top10_lift_share", np.nan),
            "one_off_imo_share": conc.get("one_off_imo_share", np.nan),
            "sts_vol_share": sts_share(df).get("sts_vol_share", np.nan),
        }
    )


def compare_flow_scopes(df: pd.DataFrame) -> pd.DataFrame:
    """Side-by-side metrics for ALL / INTER / INTRA on the same corridor load."""
    rows = []
    for scope in FLOW_SCOPES:
        part = apply_flow_scope(df, scope)
        s = scope_metrics(part)
        s["flow_scope"] = scope
        rows.append(s)
    out = pd.DataFrame(rows).set_index("flow_scope")
    return out


def dwt_bucket(dwt: float | None) -> str:
    if dwt is None or (isinstance(dwt, float) and np.isnan(dwt)):
        return "unknown"
    if dwt >= 200_000:
        return "VLCC+"
    if dwt >= 120_000:
        return "Suezmax"
    if dwt >= 80_000:
        return "Aframax/LR2"
    if dwt >= 55_000:
        return "Panamax/LR1"
    return "smaller"


def monthly_export_mbd(df: pd.DataFrame) -> pd.DataFrame:
    """Origin-month export rate in mbd."""
    if df.empty:
        return pd.DataFrame(columns=["month", "cargoes", "volume_bbl", "mbd"])
    g = (
        df.dropna(subset=["month"])
        .groupby("month", as_index=False)
        .agg(cargoes=("trade_id", "nunique"), volume_bbl=("volume_bbl", "sum"))
    )
    days = g["month"].dt.days_in_month
    g["mbd"] = g["volume_bbl"] / days / BBLS_PER_DAY_TO_MBD
    return g.sort_values("month")


def dest_mix(df: pd.DataFrame, *, top_n: int = 12) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    g = (
        df.groupby("country_dest", as_index=False)
        .agg(cargoes=("trade_id", "nunique"), volume_bbl=("volume_bbl", "sum"))
        .sort_values("volume_bbl", ascending=False)
    )
    g["share_vol"] = g["volume_bbl"] / g["volume_bbl"].sum()
    return g.head(top_n)


def dest_mix_monthly(df: pd.DataFrame, countries: list[str] | None = None) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    d = df.copy()
    if countries:
        d["dest_bucket"] = np.where(d["country_dest"].isin(countries), d["country_dest"], "Other")
    else:
        top = (
            d.groupby("country_dest")["volume_bbl"].sum().sort_values(ascending=False).head(6).index
        )
        d["dest_bucket"] = np.where(d["country_dest"].isin(top), d["country_dest"], "Other")
    g = (
        d.groupby(["month", "dest_bucket"], as_index=False)
        .agg(volume_bbl=("volume_bbl", "sum"))
    )
    tot = g.groupby("month")["volume_bbl"].transform("sum")
    g["share"] = g["volume_bbl"] / tot.replace(0, np.nan)
    return g.sort_values(["month", "dest_bucket"])


def installation_rank(df: pd.DataFrame, *, top_n: int = 15) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    g = (
        df.groupby("installation_origin", as_index=False)
        .agg(cargoes=("trade_id", "nunique"), volume_bbl=("volume_bbl", "sum"))
        .sort_values("volume_bbl", ascending=False)
    )
    g["share_vol"] = g["volume_bbl"] / g["volume_bbl"].sum()
    return g.head(top_n)


def vessel_recurrence(df: pd.DataFrame, *, top_n: int = 25) -> pd.DataFrame:
    """How often the same IMO lifts from this corridor."""
    if df.empty:
        return pd.DataFrame()
    g = (
        df.dropna(subset=["imo"])
        .groupby("imo", as_index=False)
        .agg(
            vessel=("vessel", _mode_or_na),
            lifts=("trade_id", "nunique"),
            volume_bbl=("volume_bbl", "sum"),
            vessel_dwt=("vessel_dwt", "median"),
            vessel_type=("vessel_type", _mode_or_na),
            vessel_owner=("vessel_owner", _mode_or_na),
            first_lift=("date_origin", "min"),
            last_lift=("date_origin", "max"),
        )
        .sort_values("lifts", ascending=False)
    )
    g["share_lifts"] = g["lifts"] / g["lifts"].sum()
    g["share_vol"] = g["volume_bbl"] / g["volume_bbl"].sum()
    return g.head(top_n)


def fleet_concentration(df: pd.DataFrame) -> pd.Series:
    """Herfindahl-style + top-N coverage on unique lifts by IMO."""
    if df.empty or df["imo"].dropna().empty:
        return pd.Series(dtype=float)
    lifts = df.dropna(subset=["imo"]).groupby("imo")["trade_id"].nunique()
    shares = lifts / lifts.sum()
    hhi = float((shares**2).sum())
    out = {
        "n_trades": int(df["trade_id"].nunique()),
        "n_unique_imo": int(lifts.shape[0]),
        "lifts_per_imo_mean": float(lifts.mean()),
        "lifts_per_imo_median": float(lifts.median()),
        "top10_lift_share": float(shares.sort_values(ascending=False).head(10).sum()),
        "top25_lift_share": float(shares.sort_values(ascending=False).head(25).sum()),
        "hhi_imo": hhi,
        "one_off_imo_share": float((lifts == 1).mean()),
    }
    return pd.Series(out)


def size_mix(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    g = (
        df.groupby("dwt_bucket", as_index=False)
        .agg(cargoes=("trade_id", "nunique"), volume_bbl=("volume_bbl", "sum"))
        .sort_values("volume_bbl", ascending=False)
    )
    g["share_vol"] = g["volume_bbl"] / g["volume_bbl"].sum()
    return g


def size_mix_monthly(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    g = (
        df.groupby(["month", "dwt_bucket"], as_index=False)
        .agg(volume_bbl=("volume_bbl", "sum"))
    )
    tot = g.groupby("month")["volume_bbl"].transform("sum")
    g["share"] = g["volume_bbl"] / tot.replace(0, np.nan)
    return g


def size_mix_monthly_tanker_count(df: pd.DataFrame) -> pd.DataFrame:
    """Unique mother IMOs (and cargo lifts) per origin month × DWT bucket."""
    if df.empty:
        return pd.DataFrame()
    return (
        df.groupby(["month", "dwt_bucket"], as_index=False)
        .agg(n_tankers=("imo", "nunique"), cargoes=("trade_id", "nunique"))
        .sort_values(["month", "dwt_bucket"])
    )


def _coerce_month(month: Any) -> pd.Timestamp:
    """Normalize inspect month to month-start Timestamp (matches ``df['month']``)."""
    return pd.Timestamp(month).to_period("M").to_timestamp()


def mother_month_cargoes(df: pd.DataFrame, month: Any) -> pd.DataFrame:
    """Trade-level mother rows for one origin month (reconcile tankers vs cargoes)."""
    if df.empty:
        return pd.DataFrame()
    m = _coerce_month(month)
    d = df.loc[df["month"] == m].copy()
    if d.empty:
        return d
    # voyage length helps judge whether a same-month reload is plausible
    d["voyage_days"] = (
        pd.to_datetime(d["date_dest"], errors="coerce") - d["date_origin"]
    ).dt.days
    d["imo_lifts_in_month"] = d.groupby("imo")["trade_id"].transform("nunique")
    cols = [
        "date_origin",
        "date_dest",
        "voyage_days",
        "trade_id",
        "vessel",
        "imo",
        "dwt_bucket",
        "vessel_dwt",
        "volume_bbl",
        "installation_origin",
        "destination",
        "country_dest",
        "region_dest",
        "has_sts",
        "zone_sts_1",
        "vessel_2",
        "imo_2",
        "dwt_bucket_2",
        "imo_lifts_in_month",
        "grade",
    ]
    cols = [c for c in cols if c in d.columns]
    return d[cols].sort_values(
        ["imo_lifts_in_month", "imo", "date_origin"], ascending=[False, True, True]
    )


def mother_month_repeat_tankers(df: pd.DataFrame, month: Any) -> pd.DataFrame:
    """Mothers with >1 cargo in the origin month — why ``n_tankers < cargoes``."""
    d = mother_month_cargoes(df, month)
    if d.empty:
        return pd.DataFrame()
    g = (
        d.groupby(["imo", "vessel", "dwt_bucket"], as_index=False)
        .agg(
            lifts=("trade_id", "nunique"),
            volume_bbl=("volume_bbl", "sum"),
            first_origin=("date_origin", "min"),
            last_origin=("date_origin", "max"),
            n_dest=("country_dest", "nunique"),
            top_dest=("country_dest", _mode_or_na),
            median_voyage_days=("voyage_days", "median"),
            n_sts=("has_sts", "sum"),
        )
    )
    g["gap_days"] = (g["last_origin"] - g["first_origin"]).dt.days
    return g.loc[g["lifts"] > 1].sort_values(["lifts", "volume_bbl"], ascending=False)


def owner_rank(df: pd.DataFrame, *, top_n: int = 20) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    g = (
        df.groupby("vessel_owner", as_index=False)
        .agg(
            cargoes=("trade_id", "nunique"),
            volume_bbl=("volume_bbl", "sum"),
            n_imo=("imo", "nunique"),
        )
        .sort_values("volume_bbl", ascending=False)
    )
    g["share_vol"] = g["volume_bbl"] / g["volume_bbl"].sum()
    return g.head(top_n)


def loading_cadence(df: pd.DataFrame) -> pd.DataFrame:
    """Cargoes per origin-day (busy load days) + weekday mix."""
    if df.empty:
        return pd.DataFrame()
    d = df.dropna(subset=["date_origin"]).copy()
    d["load_day"] = d["date_origin"].dt.normalize()
    d["weekday"] = d["date_origin"].dt.day_name()
    daily = d.groupby("load_day", as_index=False).agg(
        cargoes=("trade_id", "nunique"), volume_bbl=("volume_bbl", "sum")
    )
    return daily.sort_values("cargoes", ascending=False)


def weekday_mix(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    d = df.dropna(subset=["date_origin"]).copy()
    d["weekday"] = d["date_origin"].dt.day_name()
    g = (
        d.groupby("weekday", as_index=False)
        .agg(cargoes=("trade_id", "nunique"), volume_bbl=("volume_bbl", "sum"))
    )
    g["weekday"] = pd.Categorical(g["weekday"], categories=order, ordered=True)
    return g.sort_values("weekday")


def sts_share(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series({"sts_cargo_share": np.nan, "sts_vol_share": np.nan})
    return pd.Series(
        {
            "sts_cargo_share": float(df["has_sts"].mean()),
            "sts_vol_share": float(df.loc[df["has_sts"], "volume_bbl"].sum() / df["volume_bbl"].sum()),
        }
    )


def loyalty_curve(df: pd.DataFrame) -> pd.DataFrame:
    """Cumulative lift share captured by top-N IMOs."""
    if df.empty:
        return pd.DataFrame()
    lifts = (
        df.dropna(subset=["imo"])
        .groupby("imo")["trade_id"]
        .nunique()
        .sort_values(ascending=False)
    )
    if lifts.empty:
        return pd.DataFrame()
    cum = lifts.cumsum() / lifts.sum()
    out = pd.DataFrame({"rank": np.arange(1, len(cum) + 1), "cum_lift_share": cum.to_numpy()})
    return out


# --- Mother roles + daughter (STS vessel-2) fleet --------------------------------

def _mode_or_na(s: pd.Series):
    m = s.dropna()
    if m.empty:
        return pd.NA
    return m.mode().iloc[0]


def classify_mother_role(
    pct_inter: float,
    pct_intra: float,
    pct_sts: float,
    *,
    export_inter: float = 0.70,
    export_sts: float = 0.70,
    coastal_intra: float = 0.70,
    coastal_sts_max: float = 0.30,
    dual_min: float = 0.25,
) -> str:
    """Rule-based mother (IMO-1) role from behaviour shares."""
    if pct_inter >= export_inter and pct_sts >= export_sts:
        return "STS_EXPORT"
    if pct_intra >= coastal_intra and pct_sts < coastal_sts_max:
        return "COASTAL"
    if pct_inter >= dual_min and pct_intra >= dual_min:
        return "DUAL"
    return "OTHER"


def mother_role_features(df: pd.DataFrame, *, min_lifts: int = 5) -> pd.DataFrame:
    """Per mother IMO: INTER/INTRA/STS shares + role label.

    Pass the **unscoped** corridor frame (``df_all``) so roles reflect full behaviour.
    """
    if df.empty or df["imo"].dropna().empty:
        return pd.DataFrame()
    d = df.dropna(subset=["imo"]).copy()
    # Group by IMO only — vessel name casing can split the same ship.
    g = d.groupby("imo", as_index=False).agg(
        vessel=("vessel", _mode_or_na),
        lifts=("trade_id", "nunique"),
        volume_bbl=("volume_bbl", "sum"),
        vessel_dwt=("vessel_dwt", "median"),
        vessel_owner=("vessel_owner", _mode_or_na),
        n_inter=("flow_scope", lambda s: int((s == "INTER").sum())),
        n_intra=("flow_scope", lambda s: int((s == "INTRA").sum())),
        n_sts=("has_sts", "sum"),
        n_with_daughter=("has_daughter", "sum"),
    )
    g = g.loc[g["lifts"] >= min_lifts].copy()
    if g.empty:
        return g
    g["pct_inter"] = g["n_inter"] / g["lifts"]
    g["pct_intra"] = g["n_intra"] / g["lifts"]
    g["pct_sts"] = g["n_sts"] / g["lifts"]
    g["pct_with_daughter"] = g["n_with_daughter"] / g["lifts"]
    g["role"] = [
        classify_mother_role(i, a, s)
        for i, a, s in zip(g["pct_inter"], g["pct_intra"], g["pct_sts"])
    ]
    g["dwt_bucket"] = g["vessel_dwt"].map(dwt_bucket)
    return g.sort_values("lifts", ascending=False)


def mother_role_summary(roles: pd.DataFrame) -> pd.DataFrame:
    """Volume/lift share by mother role."""
    if roles is None or roles.empty:
        return pd.DataFrame()
    g = (
        roles.groupby("role", as_index=False)
        .agg(
            n_imo=("imo", "nunique"),
            lifts=("lifts", "sum"),
            volume_bbl=("volume_bbl", "sum"),
        )
        .sort_values("volume_bbl", ascending=False)
    )
    g["share_lifts"] = g["lifts"] / g["lifts"].sum()
    g["share_vol"] = g["volume_bbl"] / g["volume_bbl"].sum()
    return g


def attach_mother_roles(df: pd.DataFrame, roles: pd.DataFrame) -> pd.DataFrame:
    """Left-join role onto trade rows (by mother IMO)."""
    if df.empty or roles is None or roles.empty:
        out = df.copy()
        out["mother_role"] = pd.NA
        return out
    out = df.merge(roles[["imo", "role"]].rename(columns={"role": "mother_role"}), on="imo", how="left")
    return out


def daughter_trades(df: pd.DataFrame) -> pd.DataFrame:
    """Rows with a populated STS daughter (Vessel IMO 2)."""
    if df.empty or "imo_2" not in df.columns:
        return pd.DataFrame()
    return df.loc[df["imo_2"].notna()].copy()


def daughter_recurrence(df: pd.DataFrame, *, top_n: int = 30) -> pd.DataFrame:
    """How often the same daughter IMO takes cargo to final destination."""
    d = daughter_trades(df)
    if d.empty:
        return pd.DataFrame()
    g = (
        d.groupby("imo_2", as_index=False)
        .agg(
            vessel_2=("vessel_2", _mode_or_na),
            lifts=("trade_id", "nunique"),
            volume_bbl=("volume_bbl", "sum"),
            vessel_dwt_2=("vessel_dwt_2", "median"),
            vessel_type_2=("vessel_type_2", _mode_or_na),
            vessel_owner_2=("vessel_owner_2", _mode_or_na),
            n_mothers=("imo", "nunique"),
            top_dest=("country_dest", _mode_or_na),
            top_sts_zone=("zone_sts_1", _mode_or_na),
            first_lift=("date_origin", "min"),
            last_lift=("date_origin", "max"),
        )
        .sort_values("lifts", ascending=False)
    )
    g["share_lifts"] = g["lifts"] / g["lifts"].sum()
    g["share_vol"] = g["volume_bbl"] / g["volume_bbl"].sum()
    g["dwt_bucket_2"] = g["vessel_dwt_2"].map(dwt_bucket)
    return g.head(top_n)


def daughter_size_mix(df: pd.DataFrame) -> pd.DataFrame:
    d = daughter_trades(df)
    if d.empty:
        return pd.DataFrame()
    g = (
        d.groupby("dwt_bucket_2", as_index=False)
        .agg(cargoes=("trade_id", "nunique"), volume_bbl=("volume_bbl", "sum"))
        .sort_values("volume_bbl", ascending=False)
    )
    g["share_vol"] = g["volume_bbl"] / g["volume_bbl"].sum()
    return g


def daughter_size_mix_monthly(df: pd.DataFrame) -> pd.DataFrame:
    """Daughter DWT-bucket volume share by origin month (STS rows with IMO 2)."""
    d = daughter_trades(df)
    if d.empty:
        return pd.DataFrame()
    g = (
        d.groupby(["month", "dwt_bucket_2"], as_index=False)
        .agg(volume_bbl=("volume_bbl", "sum"))
    )
    tot = g.groupby("month")["volume_bbl"].transform("sum")
    g["share"] = g["volume_bbl"] / tot.replace(0, np.nan)
    return g.sort_values(["month", "dwt_bucket_2"])


def daughter_size_mix_monthly_tanker_count(df: pd.DataFrame) -> pd.DataFrame:
    """Unique daughter IMOs (and cargo lifts) per origin month × DWT bucket."""
    d = daughter_trades(df)
    if d.empty:
        return pd.DataFrame()
    return (
        d.groupby(["month", "dwt_bucket_2"], as_index=False)
        .agg(n_tankers=("imo_2", "nunique"), cargoes=("trade_id", "nunique"))
        .sort_values(["month", "dwt_bucket_2"])
    )


def daughter_month_cargoes(df: pd.DataFrame, month: Any) -> pd.DataFrame:
    """Trade-level daughter rows for one origin month (STS legs with IMO 2)."""
    d = daughter_trades(df)
    if d.empty:
        return pd.DataFrame()
    m = _coerce_month(month)
    d = d.loc[d["month"] == m].copy()
    if d.empty:
        return d
    d["voyage_days"] = (
        pd.to_datetime(d["date_dest"], errors="coerce") - d["date_origin"]
    ).dt.days
    d["imo_lifts_in_month"] = d.groupby("imo_2")["trade_id"].transform("nunique")
    cols = [
        "date_origin",
        "date_dest",
        "voyage_days",
        "trade_id",
        "vessel_2",
        "imo_2",
        "dwt_bucket_2",
        "vessel_dwt_2",
        "volume_bbl",
        "installation_origin",
        "zone_sts_1",
        "destination",
        "country_dest",
        "region_dest",
        "vessel",
        "imo",
        "dwt_bucket",
        "imo_lifts_in_month",
        "grade",
    ]
    cols = [c for c in cols if c in d.columns]
    return d[cols].sort_values(
        ["imo_lifts_in_month", "imo_2", "date_origin"], ascending=[False, True, True]
    )


def daughter_month_repeat_tankers(df: pd.DataFrame, month: Any) -> pd.DataFrame:
    """Daughters with >1 cargo in the origin month — why ``n_tankers < cargoes``."""
    d = daughter_month_cargoes(df, month)
    if d.empty:
        return pd.DataFrame()
    g = (
        d.groupby(["imo_2", "vessel_2", "dwt_bucket_2"], as_index=False)
        .agg(
            lifts=("trade_id", "nunique"),
            volume_bbl=("volume_bbl", "sum"),
            first_origin=("date_origin", "min"),
            last_origin=("date_origin", "max"),
            n_dest=("country_dest", "nunique"),
            top_dest=("country_dest", _mode_or_na),
            median_voyage_days=("voyage_days", "median"),
            n_mothers=("imo", "nunique"),
        )
    )
    g["gap_days"] = (g["last_origin"] - g["first_origin"]).dt.days
    return g.loc[g["lifts"] > 1].sort_values(["lifts", "volume_bbl"], ascending=False)


def annotate_vlcc_load_path(df: pd.DataFrame) -> pd.DataFrame:
    """Tag how cargo reaches a VLCC-scale haul (mother direct vs STS daughter).

    - ``DIRECT_VLCC_MOTHER``: mother DWT≥200k and no STS (loaded at origin)
    - ``STS_TO_VLCC_DAUGHTER``: daughter DWT≥200k after STS
    - ``VLCC_MOTHER_WITH_STS``: mother already VLCC+ but STS flagged (rare)
    - ``OTHER``: neither path
    """
    out = df.copy()
    mother_vlcc = out["dwt_bucket"] == "VLCC+"
    dau_vlcc = out["has_daughter"].fillna(False) & (out["dwt_bucket_2"] == "VLCC+")
    # Priority: direct VLCC load at origin > STS-fed daughter VLCC > VLCC mother with STS flag
    path = pd.Series("OTHER", index=out.index, dtype="object")
    path.loc[mother_vlcc & out["has_sts"]] = "VLCC_MOTHER_WITH_STS"
    path.loc[dau_vlcc] = "STS_TO_VLCC_DAUGHTER"
    path.loc[mother_vlcc & ~out["has_sts"]] = "DIRECT_VLCC_MOTHER"
    out["vlcc_load_path"] = path
    return out


def vlcc_load_path_summary(df: pd.DataFrame) -> pd.DataFrame:
    d = annotate_vlcc_load_path(df)
    if d.empty:
        return pd.DataFrame()
    g = (
        d.groupby("vlcc_load_path", as_index=False)
        .agg(cargoes=("trade_id", "nunique"), volume_bbl=("volume_bbl", "sum"))
        .sort_values("volume_bbl", ascending=False)
    )
    g["share_vol"] = g["volume_bbl"] / g["volume_bbl"].sum()
    return g


def direct_vlcc_installations(df: pd.DataFrame, *, top_n: int = 20) -> pd.DataFrame:
    """Origin installations where a VLCC mother loads with no STS (direct load)."""
    d = annotate_vlcc_load_path(df)
    d = d.loc[d["vlcc_load_path"] == "DIRECT_VLCC_MOTHER"]
    if d.empty:
        return pd.DataFrame()
    g = (
        d.groupby("installation_origin", as_index=False)
        .agg(
            cargoes=("trade_id", "nunique"),
            volume_bbl=("volume_bbl", "sum"),
            n_imo=("imo", "nunique"),
            top_dest=("country_dest", _mode_or_na),
        )
        .sort_values("volume_bbl", ascending=False)
    )
    g["share_vol"] = g["volume_bbl"] / g["volume_bbl"].sum()
    return g.head(top_n)


def sts_vlcc_daughter_installations(df: pd.DataFrame, *, top_n: int = 20) -> pd.DataFrame:
    """Origin installations that feed STS→VLCC daughter paths (shuttle load points)."""
    d = annotate_vlcc_load_path(df)
    d = d.loc[d["vlcc_load_path"] == "STS_TO_VLCC_DAUGHTER"]
    if d.empty:
        return pd.DataFrame()
    g = (
        d.groupby("installation_origin", as_index=False)
        .agg(
            cargoes=("trade_id", "nunique"),
            volume_bbl=("volume_bbl", "sum"),
            n_mothers=("imo", "nunique"),
            n_daughters=("imo_2", "nunique"),
            top_sts=("zone_sts_1", _mode_or_na),
            top_dest=("country_dest", _mode_or_na),
        )
        .sort_values("volume_bbl", ascending=False)
    )
    g["share_vol"] = g["volume_bbl"] / g["volume_bbl"].sum()
    return g.head(top_n)


def daughter_owner_rank(df: pd.DataFrame, *, top_n: int = 20) -> pd.DataFrame:
    d = daughter_trades(df)
    if d.empty:
        return pd.DataFrame()
    g = (
        d.groupby("vessel_owner_2", as_index=False)
        .agg(
            cargoes=("trade_id", "nunique"),
            volume_bbl=("volume_bbl", "sum"),
            n_imo=("imo_2", "nunique"),
        )
        .sort_values("volume_bbl", ascending=False)
    )
    g["share_vol"] = g["volume_bbl"] / g["volume_bbl"].sum()
    return g.head(top_n)


def mother_daughter_pairs(df: pd.DataFrame, *, top_n: int = 25) -> pd.DataFrame:
    """Most common mother→daughter IMO pairs on STS trades."""
    d = daughter_trades(df)
    if d.empty:
        return pd.DataFrame()
    g = (
        d.groupby(["imo", "vessel", "imo_2", "vessel_2"], as_index=False)
        .agg(
            lifts=("trade_id", "nunique"),
            volume_bbl=("volume_bbl", "sum"),
            top_dest=("country_dest", _mode_or_na),
            top_sts=("zone_sts_1", _mode_or_na),
        )
        .sort_values("lifts", ascending=False)
    )
    return g.head(top_n)


def sts_delivery_summary(df: pd.DataFrame) -> pd.Series:
    """How much of the slice is STS / has a daughter / daughter is VLCC+."""
    if df.empty:
        return pd.Series(dtype=float)
    vol = float(df["volume_bbl"].sum()) or np.nan
    sts = df["has_sts"]
    dau = df["has_daughter"]
    vlcc_dau = dau & (df["dwt_bucket_2"] == "VLCC+")
    return pd.Series(
        {
            "trades": int(df["trade_id"].nunique()),
            "sts_vol_share": float(df.loc[sts, "volume_bbl"].sum() / vol) if vol == vol else np.nan,
            "daughter_vol_share": float(df.loc[dau, "volume_bbl"].sum() / vol) if vol == vol else np.nan,
            "daughter_vlcc_vol_share": float(df.loc[vlcc_dau, "volume_bbl"].sum() / vol)
            if vol == vol
            else np.nan,
            "n_unique_mothers_sts": int(df.loc[sts, "imo"].nunique()),
            "n_unique_daughters": int(df.loc[dau, "imo_2"].nunique()),
        }
    )
