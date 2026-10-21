"""Import seasonality metrics by installation × product (for facility_type analysis)."""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from terminal_desk import explode_tags

MIN_PATTERN_MONTHS = 24
SegmentMode = Literal["primary", "tags"]


def complete_monthly_kbd(year_month: pd.Series, values: pd.Series) -> pd.Series:
    """Dense monthly kbd series; gaps inside span filled with 0."""
    s = pd.Series(values.values, index=pd.to_datetime(year_month)).sort_index()
    if s.empty:
        return s
    full_idx = pd.date_range(s.index.min(), s.index.max(), freq="MS")
    return s.reindex(full_idx, fill_value=0.0)


def yoy_month_corr(s: pd.Series) -> float:
    """Mean corr of each calendar month with the prior year (seasonal repeatability)."""
    corrs: list[float] = []
    df = s.rename("v").reset_index()
    df.columns = ["year_month", "v"]
    df["month"] = df["year_month"].dt.month
    for _, gm in df.groupby("month"):
        gm = gm.sort_values("year_month")
        if len(gm) < 3:
            continue
        with np.errstate(invalid="ignore", divide="ignore"):
            c = gm["v"].corr(gm["v"].shift(1))
        if pd.notna(c):
            corrs.append(float(c))
    return float(np.mean(corrs)) if corrs else np.nan


def climatology_cv(s: pd.Series) -> float:
    """CV of the 12-month average profile (high = strong calendar seasonality)."""
    df = s.rename("v").reset_index()
    df.columns = ["year_month", "v"]
    df["cal_month"] = df["year_month"].dt.month
    profile = df.groupby("cal_month")["v"].mean()
    mu = float(profile.mean())
    if mu <= 0:
        return np.nan
    return float(profile.std(ddof=0) / mu)


def import_seasonality_row(g: pd.DataFrame) -> pd.Series:
    """One Installation × Product import monthly slice -> seasonality metrics."""
    imp = complete_monthly_kbd(g["year_month"], g["kbd"])
    total_kb = float(g["kb_abs"].sum())
    mean_kbd = float(imp.mean()) if len(imp) else np.nan

    base = {
        "n_months": int(len(imp)),
        "total_import_kb": total_kb,
        "mean_import_kbd": mean_kbd,
        "import_cv": np.nan,
        "import_yoy_corr": np.nan,
        "climatology_cv": np.nan,
        "seasonal_label": "insufficient_history",
    }
    if len(imp) < MIN_PATTERN_MONTHS or mean_kbd <= 0:
        return pd.Series(base)

    cv = float(imp.std(ddof=0) / imp.mean()) if imp.mean() > 0 else np.nan
    yoy = yoy_month_corr(imp)
    clim = climatology_cv(imp)

    if pd.notna(yoy) and yoy >= 0.25 and pd.notna(clim) and clim >= 0.20:
        label = "seasonal"
    elif pd.notna(yoy) and yoy < 0.0 and pd.notna(cv) and cv < 0.8:
        label = "flat"
    elif pd.notna(cv) and cv >= 1.5:
        label = "irregular"
    else:
        label = "mixed"

    base.update(
        {
            "import_cv": cv,
            "import_yoy_corr": yoy,
            "climatology_cv": clim,
            "seasonal_label": label,
        }
    )
    return pd.Series(base)


def build_import_seasonality(monthly: pd.DataFrame) -> pd.DataFrame:
    """monthly must include Installation, Product, year_month, kbd, kb_abs; Import rows only."""
    imp = monthly.loc[monthly["Imp/Exp"].eq("Import")].copy()
    rows = (
        imp.groupby(["Installation", "Product"], sort=False)
        .apply(import_seasonality_row, include_groups=False)
        .reset_index()
    )
    return rows


MONTH_LABELS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def aggregate_type_product_imports(
    imports: pd.DataFrame,
    *,
    products: list[str] | None = None,
    segments: list[str] | None = None,
    segment_col: str = "segment",
) -> pd.DataFrame:
    """Sum import kbd by segment × Product × month."""
    df = imports.copy()
    df["year_month"] = pd.to_datetime(df["year_month"])
    if segment_col not in df.columns and "facility_type" in df.columns:
        segment_col = "facility_type"
    if products:
        df = df.loc[df["Product"].isin(products)]
    if segments:
        df = df.loc[df[segment_col].isin(segments)]
    out = (
        df.groupby([segment_col, "Product", "year_month"], as_index=False)["kbd"]
        .sum()
        .sort_values([segment_col, "Product", "year_month"])
    )
    if segment_col != "segment":
        out = out.rename(columns={segment_col: "segment"})
    out["year"] = out["year_month"].dt.year.astype(int)
    out["cal_month"] = out["year_month"].dt.month.astype(int)
    out["month_label"] = out["cal_month"].map(lambda m: MONTH_LABELS[m - 1])
    return out


def build_segment_import_series(
    imports: pd.DataFrame,
    mapped: pd.DataFrame,
    *,
    mode: SegmentMode = "tags",
    focus: list[str] | None = None,
    products: list[str] | None = None,
    min_pair_kb: float = 5_000.0,
) -> dict:
    """Build segment × product monthly series for §6-style plots.

    Parameters
    ----------
    mode:
        ``\"tags\"`` — multi-label ``tag_*`` membership (a site can appear in
        several segments; do not sum segments for a country total).
        ``\"primary\"`` — exclusive ``primary_tag`` (each site once; volumes add up).
    focus:
        Segment names to keep (tag names or primary_tag values). ``None`` keeps all.
    products:
        Product filter for the monthly series and active-pair floor.
    min_pair_kb:
        Lifetime import kb floor for a segment×product pair to be plotted.
    """
    if mode not in ("primary", "tags"):
        raise ValueError(f"mode must be 'primary' or 'tags', got {mode!r}")

    if mode == "tags":
        # Why: explode so storage+bunkering sites contribute to both segment series.
        tag_map = explode_tags(mapped)
        if focus:
            tag_map = tag_map.loc[tag_map["tag"].isin(focus)]
        segmented = imports.merge(
            tag_map[["installation_name", "tag"]],
            left_on="Installation",
            right_on="installation_name",
            how="inner",
        ).rename(columns={"tag": "segment"})
        label = "tag"
        note = "multi-tag sites are counted in each matching tag"
    else:
        # Why: exclusive bucket — same plot shape as tags mode without double counting.
        if "primary_tag" not in imports.columns:
            raise KeyError("imports needs a primary_tag column for mode='primary'")
        segmented = imports.loc[
            imports["primary_tag"].notna()
            & imports["primary_tag"].astype(str).str.strip().ne("")
            & imports["primary_tag"].astype(str).str.strip().ne("nan")
        ].copy()
        if focus:
            segmented = segmented.loc[segmented["primary_tag"].isin(focus)]
        segmented["segment"] = segmented["primary_tag"]
        label = "primary"
        note = "each site counted once under its primary_tag"

    segments = focus or sorted(
        segmented["segment"].dropna().astype(str).unique().tolist()
    )
    type_product_ts = aggregate_type_product_imports(
        segmented,
        products=products,
        segments=segments,
        segment_col="segment",
    )

    pair_src = segmented
    if products:
        pair_src = pair_src.loc[pair_src["Product"].isin(products)]
    pair_kb = (
        pair_src.groupby(["segment", "Product"])["kb_abs"]
        .sum()
        .reset_index(name="total_kb")
    )
    active_pairs = pair_kb.loc[pair_kb["total_kb"] >= min_pair_kb].copy()
    active_segments = active_pairs["segment"].unique().tolist()
    current_year = (
        int(pd.to_datetime(imports["year_month"]).dt.year.max())
        if len(imports)
        else None
    )

    return {
        "mode": mode,
        "label": label,
        "note": note,
        "type_product_ts": type_product_ts,
        "active_pairs": active_pairs,
        "active_segments": active_segments,
        # Alias kept so existing notebook loops can stay on active_tags.
        "active_tags": active_segments,
        "segmented_imports": segmented,
        "current_year": current_year,
    }


def _year_palette(years: list[int], current_year: int) -> dict[int, str]:
    """Muted greys/blues for history; current year gets red elsewhere."""
    past = [y for y in years if y != current_year]
    palette: dict[int, str] = {current_year: "#dc2626"}
    greys = ["#94a3b8", "#64748b", "#475569", "#334155", "#cbd5e1", "#78716c", "#9ca3af"]
    for i, y in enumerate(sorted(past)):
        palette[y] = greys[i % len(greys)]
    return palette


def plot_type_product_timeseries(
    ts: pd.DataFrame,
    *,
    segment: str,
    products: list[str],
    title: str,
    min_months: int = 12,
    ncols: int = 2,
) -> go.Figure:
    """One figure per segment (tag or primary_tag): monthly kbd, one subplot per product.

    Shares the same date window across panels so thin series cannot auto-zoom
    the x-axis. Products with fewer than ``min_months`` of activity are skipped.
    """
    sl = ts.loc[ts["segment"].eq(segment)].copy()
    sl["year_month"] = pd.to_datetime(sl["year_month"])
    avail: list[str] = []
    for product in products:
        n_months = sl.loc[sl["Product"].eq(product), "year_month"].nunique()
        if n_months >= min_months:
            avail.append(product)
    if not avail:
        raise ValueError(
            f"No products with ≥{min_months} months for segment={segment!r}"
        )

    x_min = sl.loc[sl["Product"].isin(avail), "year_month"].min()
    x_max = sl.loc[sl["Product"].isin(avail), "year_month"].max()
    full_idx = pd.date_range(x_min, x_max, freq="MS")

    ncols = min(ncols, len(avail))
    nrows = (len(avail) + ncols - 1) // ncols
    # Plotly spacing is a fraction of *total* height; keep it small with many rows.
    vspace = min(0.10, 0.28 / nrows)
    fig = make_subplots(
        rows=nrows,
        cols=ncols,
        subplot_titles=avail,
        shared_xaxes=True,
        vertical_spacing=vspace,
        horizontal_spacing=0.08,
    )

    for i, product in enumerate(avail):
        row, col = i // ncols + 1, i % ncols + 1
        prod = (
            sl.loc[sl["Product"].eq(product), ["year_month", "kbd"]]
            .drop_duplicates("year_month")
            .set_index("year_month")
            .reindex(full_idx)
        )
        fig.add_trace(
            go.Scatter(
                x=prod.index,
                y=prod["kbd"],
                mode="lines",
                line=dict(color="#2563eb", width=1.8),
                name=product,
                showlegend=False,
                connectgaps=False,
                hovertemplate="%{x|%b %Y}<br>%{y:.1f} kbd<extra></extra>",
            ),
            row=row,
            col=col,
        )
        fig.update_yaxes(
            title_text="kbd" if col == 1 else "",
            rangemode="tozero",
            nticks=5,
            row=row,
            col=col,
        )
        fig.update_xaxes(
            range=[x_min, x_max],
            showticklabels=(row == nrows),
            row=row,
            col=col,
        )

    fig.update_layout(
        title=title,
        height=max(380, 220 * nrows + 80),
        template="plotly_white",
        margin=dict(l=60, r=24, t=72, b=48),
    )
    return fig


def plot_bafa_seasonality(
    ts: pd.DataFrame,
    *,
    segment: str,
    products: list[str],
    title: str,
    current_year: int | None = None,
    ncols: int = 2,
) -> go.Figure:
    """BAFA-style: one subplot per product, calendar month on x, one line per year."""
    sl = ts.loc[ts["segment"].eq(segment) & ts["Product"].isin(products)].copy()
    avail_products = [p for p in products if p in set(sl["Product"])]
    if not avail_products:
        raise ValueError(f"No products for segment={segment!r}")

    current_year = int(current_year or sl["year"].max())
    years = sorted(sl["year"].unique())
    colors = _year_palette(years, current_year)

    nrows = (len(avail_products) + ncols - 1) // ncols
    fig = make_subplots(
        rows=nrows,
        cols=ncols,
        subplot_titles=avail_products,
        vertical_spacing=0.14,
        horizontal_spacing=0.08,
    )

    for i, product in enumerate(avail_products):
        row, col = i // ncols + 1, i % ncols + 1
        prod = sl.loc[sl["Product"].eq(product)]
        for year in years:
            ys = prod.loc[prod["year"] == year].sort_values("cal_month")
            if ys.empty:
                continue
            is_current = year == current_year
            fig.add_trace(
                go.Scatter(
                    x=ys["month_label"],
                    y=ys["kbd"],
                    mode="lines+markers",
                    name=str(year),
                    legendgroup=str(year),
                    showlegend=(i == 0),
                    line=dict(
                        color=colors[year],
                        width=3.5 if is_current else 1.5,
                    ),
                    marker=dict(size=4 if is_current else 3),
                    opacity=1.0 if is_current else 0.85,
                ),
                row=row,
                col=col,
            )
        fig.update_xaxes(
            categoryorder="array",
            categoryarray=MONTH_LABELS,
            row=row,
            col=col,
        )
        fig.update_yaxes(title_text="kbd", row=row, col=col)

    fig.update_layout(
        title=title,
        height=max(360, 280 * nrows),
        template="plotly_white",
        legend=dict(title="Year", orientation="h", yanchor="top", y=-0.08, x=0),
    )
    return fig
