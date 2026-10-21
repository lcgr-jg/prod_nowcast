"""
Flow visualisation helpers for prod_nowcast.

Hub Sankeys, product detail panels, and zone-level flow maps. Used by
``notebooks/05_flow_viz.ipynb``; notebook 04 remains the batch pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import duckdb
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from kpler_store import DB_PATH

from terminal_desk import DESK_MAP_PATH, owners_view

Grain = Literal["installation", "complex", "zone"]
FlowScope = Literal["domestic", "all_spain_touched", "cross_border"]
LayoutMode = Literal["dual", "separate"]
CargoGrain = Literal["product", "group", "family"]

CARGO_SQL_COL: dict[CargoGrain, str] = {
    "product": "Product",
    "group": "Group",
    "family": "Family",
}

CARGO_LABEL: dict[CargoGrain, str] = {
    "product": "Product",
    "group": "Group",
    "family": "Family",
}

DEFAULT_OD_CARGO: dict[CargoGrain, list[str]] = {
    "product": ["Naphtha", "Gasoil", "Gasoline", "Jet", "FO"],
    "group": [
        "Gasoil/Diesel",
        "Gasoline/Naphtha",
        "Fuel Oils",
        "Kero/Jet",
        "Blendings",
    ],
    "family": ["Middle Distillates", "Light Ends", "DPP"],
}

DEFAULT_PRODUCT_COLORS = {
    "Naphtha": "#8b5cf6",
    "Gasoil": "#2f6f8f",
    "Gasoline": "#c45c26",
    "Jet": "#059669",
    "FO": "#6b7280",
    "Diesel": "#2563eb",
    "Bitumen/Asphalt": "#78716c",
}

DEFAULT_GROUP_COLORS = {
    "Gasoil/Diesel": "#2f6f8f",
    "Gasoline/Naphtha": "#c45c26",
    "Fuel Oils": "#6b7280",
    "Kero/Jet": "#059669",
    "Blendings": "#8b5cf6",
    "Resids": "#78716c",
}

DEFAULT_FAMILY_COLORS = {
    "Middle Distillates": "#2563eb",
    "Light Ends": "#c45c26",
    "DPP": "#6b7280",
    "Clean": "#9ca3af",
}

DEFAULT_FLOW_CLASS_COLORS = {
    "captive_same_owner": "#2f6f8f",
    "internal_balancing": "#1f4e66",
    "seller_buyer_match": "#6b7280",
    "island_or_coastal_supply": "#c45c26",
    "commercial_to_3rd": "#9ca3af",
}

# Desk-friendly labels for product-detail Sankey legends
FLOW_CLASS_LABELS = {
    "captive_same_owner": "Same owner (internal)",
    "internal_balancing": "Internal balancing",
    "seller_buyer_match": "Seller = buyer",
    "island_or_coastal_supply": "Coastal / island supply",
    "commercial_to_3rd": "Commercial / 3rd party",
}

DEFAULT_ISLAND_ZONES = {
    "Las Palmas",
    "Tenerife",
    "Palma",
    "Arrecife",
    "Arinaga",
    "Salinetas Port",
    "Puerto de la Estaca",
    "Santa Cruz de La Palma",
    "Puerto del Rosario",
    "San Sebastián de La Gomera",
    "Ceuta",
    "Malaga",
}


@dataclass(frozen=True)
class HubSpec:
    """Resolved drill-down target at installation, complex, or zone grain."""

    entity: str
    grain: Grain
    label: str
    members: frozenset[str] | None = None
    zone: str | None = None


def _cargo_hex(name: str, *, cargo_grain: CargoGrain = "product") -> str:
    if cargo_grain == "family":
        palette = DEFAULT_FAMILY_COLORS
    elif cargo_grain == "group":
        palette = DEFAULT_GROUP_COLORS
    else:
        palette = DEFAULT_PRODUCT_COLORS
    c = palette.get(name, "#9ca3af")
    return c if isinstance(c, str) and c.startswith("#") else "#9ca3af"


def _cargo_rgba(name: str, *, cargo_grain: CargoGrain = "product", alpha: float = 0.55) -> str:
    c = _cargo_hex(name, cargo_grain=cargo_grain)
    return (
        f"rgba({int(c[1:3], 16)},{int(c[3:5], 16)},"
        f"{int(c[5:7], 16)},{alpha})"
    )


def _product_rgba(product: str, alpha: float = 0.55) -> str:
    return _cargo_rgba(product, cargo_grain="product", alpha=alpha)


def resolve_od_cargo(
    cargo: list[str] | None,
    *,
    cargo_grain: CargoGrain,
) -> list[str] | None:
    """Return cargo filter list; None means no filter (all cargo at that grain)."""
    if cargo is not None:
        return cargo
    return DEFAULT_OD_CARGO.get(cargo_grain)


def _product_hex(product: str) -> str:
    return _cargo_hex(product, cargo_grain="product")


def _flow_class_rgba(flow_class: str, alpha: float = 0.45) -> str:
    c = DEFAULT_FLOW_CLASS_COLORS.get(flow_class, "#9ca3af")
    if not (isinstance(c, str) and c.startswith("#") and len(c) == 7):
        return f"rgba(156,163,175,{alpha})"
    return (
        f"rgba({int(c[1:3], 16)},{int(c[3:5], 16)},"
        f"{int(c[5:7], 16)},{alpha})"
    )


def _flow_class_hex(flow_class: str) -> str:
    c = DEFAULT_FLOW_CLASS_COLORS.get(flow_class, "#9ca3af")
    return c if isinstance(c, str) and c.startswith("#") else "#9ca3af"


def _add_color_legend(
    fig: go.Figure,
    items: dict[str, str],
    *,
    title: str = "",
    x: float = 1.02,
    y: float = 1.0,
    right_margin: int = 180,
    update_layout: bool = True,
) -> None:
    """Dummy marker traces — Plotly Sankey/geo traces do not support legends natively."""
    for label, color in items.items():
        fig.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="markers",
                marker=dict(size=11, color=color, symbol="square"),
                name=label,
                legendgroup=title or "legend",
                showlegend=True,
                hoverinfo="skip",
            )
        )
    if not update_layout:
        return
    fig.update_layout(
        legend=dict(
            title=dict(text=title) if title else None,
            orientation="v",
            yanchor="top",
            y=y,
            xanchor="left",
            x=x,
            bgcolor="rgba(255,255,255,0.9)",
            bordercolor="#d1d5db",
            borderwidth=1,
        ),
        margin=dict(
            l=fig.layout.margin.l if fig.layout.margin.l is not None else 20,
            r=right_margin,
            t=fig.layout.margin.t if fig.layout.margin.t is not None else 60,
            b=fig.layout.margin.b if fig.layout.margin.b is not None else 20,
        ),
    )


def load_entity_map(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["Installation"] = df["Installation"].astype(str).str.strip()
    return df


def load_owners(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "installation_name" in df.columns:
        return owners_view(df)
    df["installation"] = df["installation"].astype(str).str.strip()
    return df.drop_duplicates("installation", keep="first")


def load_zone_coords(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["zone"] = df["zone"].astype(str).str.strip()
    df["country"] = df["country"].astype(str).str.strip()
    return df


def load_roles(path: Path) -> pd.DataFrame:
    roles = pd.read_csv(path)
    return roles[
        ["Installation", "Product", "role", "import_share", "net_kb"]
    ].copy()


def resolve_entity(entity: str, grain: Grain, entity_map: pd.DataFrame) -> HubSpec:
    entity = str(entity).strip()
    if grain == "zone":
        return HubSpec(
            entity=entity,
            grain="zone",
            label=entity,
            members=None,
            zone=entity,
        )
    if grain == "complex":
        mask = entity_map["complex_id"].eq(entity) | entity_map["complex_name"].eq(
            entity
        )
        members_df = entity_map.loc[mask]
        if members_df.empty:
            raise ValueError(f"Unknown complex hub: {entity!r}")
        members = frozenset(members_df["Installation"].astype(str))
        return HubSpec(
            entity=str(members_df["complex_id"].iloc[0]),
            grain="complex",
            label=str(members_df["complex_name"].iloc[0]),
            members=members,
            zone=str(members_df["Zone"].mode().iloc[0])
            if "Zone" in members_df.columns and len(members_df)
            else None,
        )
    if grain == "installation":
        hit = entity_map.loc[entity_map["Installation"].eq(entity)]
        if hit.empty:
            return HubSpec(
                entity=entity,
                grain="installation",
                label=entity,
                members=frozenset({entity}),
                zone=None,
            )
        row = hit.iloc[0]
        return HubSpec(
            entity=entity,
            grain="installation",
            label=entity,
            members=frozenset({entity}),
            zone=str(row["Zone"]) if pd.notna(row.get("Zone")) else None,
        )
    raise ValueError(f"grain must be installation|complex|zone, got {grain!r}")


def rollup_entity_label(
    installation: str,
    grain: Grain,
    entity_map: pd.DataFrame,
    *,
    zone: str | None = None,
) -> str:
    if grain == "zone":
        return str(zone).strip() if zone and str(zone).strip() else "(unknown zone)"
    inst = str(installation).strip()
    if grain == "installation":
        return inst
    if grain == "complex":
        hit = entity_map.loc[entity_map["Installation"].eq(inst)]
        if len(hit):
            return str(hit["complex_name"].iloc[0])
        return inst
    raise ValueError(f"grain must be installation|complex|zone, got {grain!r}")


def _attach_owner(df: pd.DataFrame, owners: pd.DataFrame, inst_col: str, prefix: str) -> pd.DataFrame:
    out = df.merge(
        owners.rename(
            columns={
                "installation": inst_col,
                "owner": f"{prefix}_owner",
                "site_type": f"{prefix}_site_type",
            }
        )[[inst_col, f"{prefix}_owner", f"{prefix}_site_type"]],
        on=inst_col,
        how="left",
    )
    out[f"{prefix}_owner"] = out[f"{prefix}_owner"].fillna("(unmapped)")
    out[f"{prefix}_site_type"] = out[f"{prefix}_site_type"].fillna("(unmapped)")
    return out


def classify_od_flow(row: pd.Series, island_zones: set[str]) -> str:
    o_owner, d_owner = row["origin_owner"], row["dest_owner"]
    o_type, d_type = row["origin_site_type"], row["dest_site_type"]
    same_owner = o_owner != "(unmapped)" and o_owner == d_owner
    if same_owner and o_type == "refinery" and d_type == "refinery":
        return "internal_balancing"
    if same_owner:
        return "captive_same_owner"
    if row["seller"] not in {"(unknown)", ""} and row["seller"] == row["buyer"]:
        return "seller_buyer_match"
    if str(row["dest_zone"]) in island_zones:
        return "island_or_coastal_supply"
    return "commercial_to_3rd"


def build_od_flows(
    raw: pd.DataFrame,
    *,
    owners: pd.DataFrame,
    roles: pd.DataFrame | None = None,
    island_zones: set[str] | None = None,
    cargo_grain: CargoGrain = "product",
) -> pd.DataFrame:
    island_zones = island_zones or DEFAULT_ISLAND_ZONES
    flows = _attach_owner(raw, owners, "origin_installation", "origin")
    flows = _attach_owner(flows, owners, "dest_installation", "dest")
    flows["same_owner"] = (
        (flows["origin_owner"] != "(unmapped)")
        & (flows["origin_owner"] == flows["dest_owner"])
    )
    flows["seller_eq_buyer"] = (
        (flows["seller"] != "(unknown)") & (flows["seller"] == flows["buyer"])
    )
    flows["flow_class"] = flows.apply(
        lambda r: classify_od_flow(r, island_zones), axis=1
    )

    # Role join only at product grain (roles CSV is installation × Product).
    if cargo_grain == "product" and roles is not None and len(roles):
        role_cols = ["Installation", "Product", "role", "import_share", "net_kb"]
        roles_od = roles[role_cols].copy()
        flows = flows.merge(
            roles_od.rename(
                columns={
                    "Installation": "origin_installation",
                    "role": "origin_role",
                    "import_share": "origin_import_share",
                    "net_kb": "origin_net_kb",
                }
            ),
            on=["origin_installation", "Product"],
            how="left",
        )
        flows = flows.merge(
            roles_od.rename(
                columns={
                    "Installation": "dest_installation",
                    "role": "dest_role",
                    "import_share": "dest_import_share",
                    "net_kb": "dest_net_kb",
                }
            ),
            on=["dest_installation", "Product"],
            how="left",
        )
        flows["surplus_to_deficit"] = flows["origin_role"].eq(
            "supply_center"
        ) & flows["dest_role"].eq("demand_center")

    return flows


def _scope_sql(scope: FlowScope, focus_country: str) -> str:
    country = focus_country.replace("'", "''")
    if scope == "domestic":
        return (
            f'AND "Country (origin)" = \'{country}\' '
            f'AND "Country (destination)" = \'{country}\''
        )
    if scope == "cross_border":
        return (
            f'AND ("Country (origin)" = \'{country}\' '
            f'OR "Country (destination)" = \'{country}\') '
            f'AND "Country (origin)" <> "Country (destination)"'
        )
    # all_spain_touched — any leg touching the focus country
    return (
        f'AND ("Country (origin)" = \'{country}\' '
        f'OR "Country (destination)" = \'{country}\')'
    )


def query_od_edges_from_duckdb(
    *,
    focus_country: str,
    scope: FlowScope = "all_spain_touched",
    products: list[str] | None = None,
    cargo_grain: CargoGrain = "product",
    status: str = "Delivered",
    db_path: Path | None = None,
) -> pd.DataFrame:
    db_path = db_path or DB_PATH
    if not db_path.exists():
        raise FileNotFoundError(f"Missing {db_path}. Run bootstrap_duckdb.py first.")

    cargo_col = CARGO_SQL_COL[cargo_grain]
    cargo_filter_sql = ""
    if products:
        in_list = ", ".join("'" + p.replace("'", "''") + "'" for p in products)
        cargo_filter_sql = f'AND "{cargo_col}" IN ({in_list})'

    scope_sql = _scope_sql(scope, focus_country)
    sql = f"""
SELECT
  "Installation origin" AS origin_installation,
  "Installation Destination" AS dest_installation,
  "Country (origin)" AS origin_country,
  "Country (destination)" AS dest_country,
  "Zone Origin" AS origin_zone,
  "Zone Destination" AS dest_zone,
  COALESCE("{cargo_col}", '(unknown)') AS Product,
  COALESCE("Seller (origin)", '(unknown)') AS seller,
  COALESCE("Buyer (destination)", '(unknown)') AS buyer,
  COUNT(*) AS n_trades,
  SUM(TRY_CAST("Volume (bbl)" AS DOUBLE) / 1000.0) AS kb
FROM trades
WHERE "Trade status" = '{status.replace("'", "''")}'
  {scope_sql}
  AND "Installation origin" IS NOT NULL AND TRIM("Installation origin") <> ''
  AND "Installation Destination" IS NOT NULL AND TRIM("Installation Destination") <> ''
  AND Family <> 'Dirty'
  AND Product NOT IN ('Crude', 'Condensate')
  AND "Group" <> 'Crude/Co'
  {cargo_filter_sql}
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
"""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return con.execute(sql).df()
    finally:
        con.close()


def _flows_cache_path(
    derived_dir: Path,
    scope: FlowScope,
    cargo_grain: CargoGrain = "product",
) -> Path:
    if scope == "domestic" and cargo_grain == "product":
        return derived_dir / "owner_od_flows.csv"
    name = f"owner_od_flows_{scope}"
    if cargo_grain != "product":
        name += f"_{cargo_grain}"
    return derived_dir / f"{name}.csv"


def cache_od_flows(
    flows: pd.DataFrame,
    *,
    derived_dir: Path,
    scope: FlowScope,
    cargo_grain: CargoGrain = "product",
) -> Path:
    """Write enriched flows to derived cache (speeds up later notebook runs)."""
    derived_dir = Path(derived_dir)
    derived_dir.mkdir(parents=True, exist_ok=True)
    path = _flows_cache_path(derived_dir, scope, cargo_grain)
    flows.to_csv(path, index=False)
    return path


def load_od_flows(
    *,
    focus_country: str,
    scope: FlowScope = "all_spain_touched",
    derived_dir: Path,
    db_path: Path | None = None,
    owners_path: Path | None = None,
    roles_path: Path | None = None,
    products: list[str] | None = None,
    cargo_grain: CargoGrain = "product",
    prefer_cache: bool = True,
    island_zones: set[str] | None = None,
) -> tuple[pd.DataFrame, str]:
    """Load enriched OD flows from cache CSV or DuckDB (+ role/owner joins).

    Returns (flows, source_label). The ``Product`` column holds the active
    cargo dimension (Product, Group, or Family) aliased for downstream viz.
    """
    derived_dir = Path(derived_dir)
    cargo_filter = resolve_od_cargo(products, cargo_grain=cargo_grain)
    cache = _flows_cache_path(derived_dir, scope, cargo_grain)
    if prefer_cache and cache.exists():
        flows = pd.read_csv(cache)
        if cargo_filter:
            flows = flows.loc[flows["Product"].isin(cargo_filter)].copy()
        return flows, f"cache:{cache.name}"

    owners_path = owners_path or DESK_MAP_PATH
    roles_path = roles_path or derived_dir / "terminal_product_roles.csv"
    owners = load_owners(owners_path)
    roles = load_roles(roles_path) if roles_path.exists() else None

    raw = query_od_edges_from_duckdb(
        focus_country=focus_country,
        scope=scope,
        products=cargo_filter,
        cargo_grain=cargo_grain,
        db_path=db_path,
    )
    flows = build_od_flows(
        raw,
        owners=owners,
        roles=roles,
        island_zones=island_zones,
        cargo_grain=cargo_grain,
    )
    flows["cargo_grain"] = cargo_grain
    return flows, "duckdb"


def flows_touching_hub(flows: pd.DataFrame, hub: HubSpec) -> pd.DataFrame:
    if hub.grain == "zone":
        z = hub.zone
        return flows.loc[
            flows["origin_zone"].astype(str).eq(z)
            | flows["dest_zone"].astype(str).eq(z)
        ].copy()
    members = hub.members or frozenset()
    return flows.loc[
        flows["origin_installation"].astype(str).isin(members)
        | flows["dest_installation"].astype(str).isin(members)
    ].copy()


def build_hub_trade_edges(
    flows: pd.DataFrame,
    hub: HubSpec,
    *,
    direction: Literal["inbound", "outbound"],
    counterparty_grain: Grain,
    entity_map: pd.DataFrame,
    products: list[str] | None = None,
    top_n: int = 12,
    min_kb: float = 500.0,
) -> pd.DataFrame:
    """Aggregate external inbound/outbound legs for a hub (counterparty × product)."""
    sub = flows.loc[flows["kb"] >= min_kb].copy()
    if products:
        sub = sub.loc[sub["Product"].isin(products)]

    if hub.grain == "zone":
        hub_zone = hub.zone
        if direction == "inbound":
            sub = sub.loc[sub["dest_zone"].astype(str).eq(hub_zone)]
            sub = sub.loc[~sub["origin_zone"].astype(str).eq(hub_zone)]
            cp_inst, cp_zone = "origin_installation", "origin_zone"
        else:
            sub = sub.loc[sub["origin_zone"].astype(str).eq(hub_zone)]
            sub = sub.loc[~sub["dest_zone"].astype(str).eq(hub_zone)]
            cp_inst, cp_zone = "dest_installation", "dest_zone"
    else:
        members = hub.members or frozenset()
        if direction == "inbound":
            sub = sub.loc[sub["dest_installation"].astype(str).isin(members)]
            sub = sub.loc[~sub["origin_installation"].astype(str).isin(members)]
            cp_inst, cp_zone = "origin_installation", "origin_zone"
        else:
            sub = sub.loc[sub["origin_installation"].astype(str).isin(members)]
            sub = sub.loc[~sub["dest_installation"].astype(str).isin(members)]
            cp_inst, cp_zone = "dest_installation", "dest_zone"

    if sub.empty:
        return pd.DataFrame(columns=["counterparty", "Product", "kb", "n_trades"])

    sub["counterparty"] = [
        rollup_entity_label(
            str(i),
            counterparty_grain,
            entity_map,
            zone=str(z),
        )
        for i, z in zip(sub[cp_inst], sub[cp_zone], strict=True)
    ]
    agg = (
        sub.groupby(["counterparty", "Product"], as_index=False)
        .agg(kb=("kb", "sum"), n_trades=("n_trades", "sum"))
        .sort_values("kb", ascending=False)
    )
    return agg.head(top_n).reset_index(drop=True)


def aggregate_zone_edges(
    flows: pd.DataFrame,
    *,
    focus_country: str,
    product: str | None = None,
    min_kb: float = 500.0,
    top_n: int | None = 25,
) -> pd.DataFrame:
    """Roll installation-level flows up to zone → zone for mapping."""
    sub = flows.loc[flows["kb"] >= min_kb].copy()
    if product:
        sub = sub.loc[sub["Product"].eq(product)]

    agg = (
        sub.groupby(
            [
                "origin_zone",
                "dest_zone",
                "origin_country",
                "dest_country",
                "Product",
            ],
            as_index=False,
        )
        .agg(kb=("kb", "sum"), n_trades=("n_trades", "sum"))
        .sort_values("kb", ascending=False)
    )
    # Keep edges that touch the focus country on at least one end
    touch = agg["origin_country"].eq(focus_country) | agg["dest_country"].eq(
        focus_country
    )
    agg = agg.loc[touch].reset_index(drop=True)
    if top_n is not None:
        agg = agg.head(top_n).reset_index(drop=True)
    return agg


def plot_od_sankey(
    edges: pd.DataFrame,
    *,
    title: str,
    top_n: int = 25,
    source_col: str = "origin_installation",
    target_col: str = "dest_installation",
    value_col: str = "kb",
    label_col: str = "Product",
    show_country: bool = False,
) -> go.Figure:
    plot_df = edges.nlargest(top_n, value_col).copy()
    if plot_df.empty:
        fig = go.Figure()
        fig.update_layout(title=title + " (no edges)")
        return fig

    def _node_label(inst: str, owner: str, country: str | None = None) -> str:
        base = f"{inst} [{owner}]" if owner and owner != "(unmapped)" else str(inst)
        if show_country and country:
            return f"{base} ({country})"
        return base

    nodes = pd.Index(
        pd.concat([plot_df[source_col], plot_df[target_col]], ignore_index=True).unique()
    )
    node_map = {n: i for i, n in enumerate(nodes)}

    owner_lookup: pd.Series | dict = {}
    if "origin_owner" in plot_df.columns:
        owner_lookup = (
            pd.concat(
                [
                    plot_df[[source_col, "origin_owner"]].rename(
                        columns={source_col: "node", "origin_owner": "owner"}
                    ),
                    plot_df[[target_col, "dest_owner"]].rename(
                        columns={target_col: "node", "dest_owner": "owner"}
                    ),
                ],
                ignore_index=True,
            )
            .drop_duplicates("node")
            .set_index("node")["owner"]
        )

    country_lookup: dict = {}
    if show_country and "origin_country" in plot_df.columns:
        country_lookup = (
            pd.concat(
                [
                    plot_df[[source_col, "origin_country"]].rename(
                        columns={source_col: "node", "origin_country": "country"}
                    ),
                    plot_df[[target_col, "dest_country"]].rename(
                        columns={target_col: "node", "dest_country": "country"}
                    ),
                ],
                ignore_index=True,
            )
            .drop_duplicates("node")
            .set_index("node")["country"]
            .to_dict()
        )

    labels = [
        _node_label(
            n,
            owner_lookup.get(n, "(unmapped)") if len(owner_lookup) else "(unmapped)",
            country_lookup.get(n),
        )
        for n in nodes
    ]

    link_colors = []
    for fc in plot_df.get("flow_class", pd.Series(["commercial_to_3rd"] * len(plot_df))):
        link_colors.append(_flow_class_rgba(str(fc)))

    fig = go.Figure(
        data=[
            go.Sankey(
                arrangement="snap",
                node=dict(
                    pad=14,
                    thickness=14,
                    line=dict(color="#374151", width=0.5),
                    label=labels,
                    color="#e5e7eb",
                ),
                link=dict(
                    source=plot_df[source_col].map(node_map),
                    target=plot_df[target_col].map(node_map),
                    value=plot_df[value_col],
                    label=(
                        plot_df[label_col].astype(str)
                        + " · "
                        + plot_df.get("flow_class", "flow").astype(str)
                        + " · "
                        + plot_df[value_col].round(0).astype(int).astype(str)
                        + " kb"
                    ),
                    color=link_colors,
                ),
            )
        ]
    )
    fig.update_layout(
        title=title,
        font=dict(size=11),
        height=650,
        margin=dict(l=20, r=20, t=60, b=20),
    )

    if "flow_class" in plot_df.columns:
        classes = plot_df["flow_class"].dropna().astype(str).unique().tolist()
        legend_items = {
            FLOW_CLASS_LABELS.get(c, c): _flow_class_hex(c) for c in sorted(classes)
        }
        _add_color_legend(fig, legend_items, title="Flow type", x=1.02, y=1.0)
        fig.update_layout(margin=dict(l=20, r=180, t=60, b=20))

    return fig


def plot_hub_trade_sankey(
    hub_label: str,
    inbound: pd.DataFrame,
    outbound: pd.DataFrame,
    *,
    title: str,
    cargo_grain: CargoGrain = "product",
) -> go.Figure:
    """Bipartite Sankey: partners → hub → partners (supports zone counterparty labels)."""
    if inbound.empty and outbound.empty:
        fig = go.Figure()
        fig.update_layout(title=title + " (no edges)")
        return fig

    left_nodes = inbound["counterparty"].drop_duplicates().tolist() if len(inbound) else []
    right_nodes = (
        outbound["counterparty"].drop_duplicates().tolist() if len(outbound) else []
    )
    nodes = left_nodes + [hub_label] + right_nodes
    hub_idx = len(left_nodes)
    node_map = {n: i for i, n in enumerate(nodes)}

    sources, targets, values, link_labels, link_colors = [], [], [], [], []

    for _, row in inbound.iterrows():
        sources.append(node_map[row["counterparty"]])
        targets.append(hub_idx)
        values.append(row["kb"])
        link_labels.append(
            f"{row['Product']} · {row['kb']:,.0f} kb · {int(row['n_trades'])} trades"
        )
        link_colors.append(
            _cargo_rgba(str(row["Product"]), cargo_grain=cargo_grain)
        )

    for _, row in outbound.iterrows():
        sources.append(hub_idx)
        targets.append(node_map[row["counterparty"]])
        values.append(row["kb"])
        link_labels.append(
            f"{row['Product']} · {row['kb']:,.0f} kb · {int(row['n_trades'])} trades"
        )
        link_colors.append(
            _cargo_rgba(str(row["Product"]), cargo_grain=cargo_grain)
        )

    fig = go.Figure(
        data=[
            go.Sankey(
                arrangement="snap",
                node=dict(
                    pad=16,
                    thickness=16,
                    line=dict(color="#374151", width=0.5),
                    label=nodes,
                    color=["#dbeafe"] * len(left_nodes)
                    + ["#fcd34d"]
                    + ["#dcfce7"] * len(right_nodes),
                ),
                link=dict(
                    source=sources,
                    target=targets,
                    value=values,
                    label=link_labels,
                    color=link_colors,
                ),
            )
        ]
    )
    fig.update_layout(
        title=title,
        font=dict(size=11),
        height=700,
        margin=dict(l=20, r=20, t=60, b=20),
    )

    products_used = sorted(
        set(inbound.get("Product", pd.Series(dtype=str)).astype(str).tolist())
        | set(outbound.get("Product", pd.Series(dtype=str)).astype(str).tolist())
    )
    if products_used:
        legend_items = {
            p: _cargo_hex(p, cargo_grain=cargo_grain) for p in products_used
        }
        _add_color_legend(
            fig,
            legend_items,
            title=CARGO_LABEL[cargo_grain],
            x=1.02,
            y=1.0,
        )
        fig.update_layout(margin=dict(l=20, r=180, t=60, b=20))

    return fig


def plot_hub_trade_bars(
    inbound: pd.DataFrame,
    outbound: pd.DataFrame,
    *,
    title: str,
    top_partners: int = 6,
    cargo_grain: CargoGrain = "product",
) -> go.Figure:
    rows: list[dict] = []
    products = sorted(
        set(inbound.get("Product", pd.Series(dtype=str)).tolist())
        | set(outbound.get("Product", pd.Series(dtype=str)).tolist())
    )

    for prod in products:
        for direction, df, sign in (
            ("inbound", inbound, -1),
            ("outbound", outbound, 1),
        ):
            sub = df.loc[df["Product"].eq(prod)] if len(df) else df
            if sub.empty:
                continue
            top = (
                sub.groupby("counterparty", as_index=False)["kb"]
                .sum()
                .nlargest(top_partners, "kb")
            )
            for _, row in top.iterrows():
                arrow = "←" if direction == "inbound" else "→"
                rows.append(
                    {
                        "y_label": f"{prod} {arrow} {row['counterparty']}",
                        "x": sign * row["kb"],
                        "kb": row["kb"],
                        "Product": prod,
                    }
                )

    if not rows:
        fig = go.Figure()
        fig.update_layout(title=title + " (no edges)")
        return fig

    plot_df = pd.DataFrame(rows).sort_values("x")
    colors = [
        _cargo_rgba(p, cargo_grain=cargo_grain, alpha=0.8)
        for p in plot_df["Product"]
    ]

    fig = go.Figure(
        go.Bar(
            y=plot_df["y_label"],
            x=plot_df["x"],
            orientation="h",
            marker_color=colors,
            text=plot_df["kb"].map(lambda v: f"{v:,.0f} kb"),
            textposition="outside",
            hovertemplate="%{y}<br>%{text}<extra></extra>",
        )
    )
    fig.update_layout(
        title=title,
        xaxis_title="kb (inbound ← | → outbound)",
        yaxis_title="",
        height=max(420, 28 * len(plot_df)),
        margin=dict(l=20, r=20, t=60, b=20),
    )
    fig.add_vline(x=0, line_width=1, line_color="#374151")
    return fig


def plot_product_flow_sankey(
    flows: pd.DataFrame,
    *,
    product: str,
    hub: HubSpec | None = None,
    display_grain: Grain = "zone",
    top_n: int = 20,
    min_kb: float = 500.0,
    title: str | None = None,
) -> go.Figure:
    """Single-product detail panel; rolls up to zone (or installation) for readability."""
    sub = flows.loc[
        flows["Product"].eq(product) & (flows["kb"] >= min_kb)
    ].copy()
    if hub is not None:
        sub = flows_touching_hub(sub, hub)
    if sub.empty:
        fig = go.Figure()
        fig.update_layout(title=(title or product) + " (no edges)")
        return fig

    if display_grain == "zone":
        plot_edges = (
            sub.groupby(
                ["origin_zone", "dest_zone", "Product"],
                as_index=False,
            )
            .agg(
                kb=("kb", "sum"),
                n_trades=("n_trades", "sum"),
                flow_class=(
                    "flow_class",
                    lambda s: s.mode().iloc[0] if len(s.mode()) else "commercial_to_3rd",
                ),
            )
            .sort_values("kb", ascending=False)
        )
        source_col, target_col = "origin_zone", "dest_zone"
    else:
        plot_edges = sub.sort_values("kb", ascending=False)
        source_col, target_col = "origin_installation", "dest_installation"

    hub_part = f" · {hub.label}" if hub else ""
    fig = plot_od_sankey(
        plot_edges,
        title=title
        or f"{product}{hub_part} — top {top_n} edges (>={min_kb:g} kb)",
        top_n=top_n,
        source_col=source_col,
        target_col=target_col,
    )
    return fig


def _arc_points(
    lat0: float,
    lon0: float,
    lat1: float,
    lon1: float,
    n: int = 30,
) -> tuple[list[float], list[float]]:
    """Quadratic bezier arc for flow lines on a map."""
    lat0, lon0, lat1, lon1 = map(float, (lat0, lon0, lat1, lon1))
    mid_lat = (lat0 + lat1) / 2.0
    mid_lon = (lon0 + lon1) / 2.0
    dx, dy = lon1 - lon0, lat1 - lat0
    dist = max((dx * dx + dy * dy) ** 0.5, 1e-6)
    # Offset control point perpendicular to chord (scaled for visible curve)
    ctrl_lat = mid_lat + 0.35 * dx
    ctrl_lon = mid_lon - 0.35 * dy * max(1.0, 1.5 / dist)
    t = np.linspace(0, 1, n)
    lats = (1 - t) ** 2 * lat0 + 2 * (1 - t) * t * ctrl_lat + t**2 * lat1
    lons = (1 - t) ** 2 * lon0 + 2 * (1 - t) * t * ctrl_lon + t**2 * lon1
    return lats.tolist(), lons.tolist()


def _zone_coord_lookup(zone_coords: pd.DataFrame) -> dict[str, tuple[float, float]]:
    return {
        str(r.zone): (float(r.lat), float(r.lon))
        for r in zone_coords.itertuples()
        if pd.notna(r.lat) and pd.notna(r.lon)
    }


def plot_zone_flow_map(
    zone_edges: pd.DataFrame,
    zone_coords: pd.DataFrame,
    *,
    focus_country: str,
    title: str,
    highlight_zones: set[str] | None = None,
    cargo_grain: CargoGrain = "product",
) -> go.Figure:
    """Origin→destination arcs between Kpler zones (line width ∝ kb)."""
    coords = _zone_coord_lookup(zone_coords)
    highlight_zones = highlight_zones or set()

    fig = go.Figure()
    max_kb = float(zone_edges["kb"].max()) if len(zone_edges) else 1.0

    for _, row in zone_edges.iterrows():
        oz, dz = str(row["origin_zone"]), str(row["dest_zone"])
        if oz not in coords or dz not in coords:
            continue
        lat0, lon0 = coords[oz]
        lat1, lon1 = coords[dz]
        lats, lons = _arc_points(lat0, lon0, lat1, lon1)
        width = 1.0 + 8.0 * float(row["kb"]) / max_kb
        color = _cargo_rgba(str(row["Product"]), cargo_grain=cargo_grain, alpha=0.65)
        fig.add_trace(
            go.Scattergeo(
                lon=lons,
                lat=lats,
                mode="lines",
                line=dict(width=width, color=color),
                hovertemplate=(
                    f"{oz} → {dz}<br>{row['Product']}<br>"
                    f"{row['kb']:,.0f} kb<extra></extra>"
                ),
                showlegend=False,
            )
        )

    used_zones = set(zone_edges["origin_zone"].astype(str)) | set(
        zone_edges["dest_zone"].astype(str)
    )
    node_rows = []
    for z in sorted(used_zones):
        if z not in coords:
            continue
        lat, lon = coords[z]
        kb = float(
            zone_edges.loc[
                zone_edges["origin_zone"].astype(str).eq(z)
                | zone_edges["dest_zone"].astype(str).eq(z),
                "kb",
            ].sum()
        )
        node_rows.append({"zone": z, "lat": lat, "lon": lon, "kb": kb})

    if node_rows:
        nodes = pd.DataFrame(node_rows)
        focus_mask = nodes["zone"].isin(highlight_zones) if highlight_zones else pd.Series(
            [False] * len(nodes)
        )
        fig.add_trace(
            go.Scattergeo(
                lon=nodes["lon"],
                lat=nodes["lat"],
                text=nodes["zone"],
                mode="markers+text",
                textposition="top center",
                marker=dict(
                    size=np.clip(6 + nodes["kb"] ** 0.5 / 80, 8, 28),
                    color=np.where(focus_mask, "#c45c26", "#2f6f8f"),
                    line=dict(width=1, color="#111827"),
                ),
                hovertemplate="%{text}<br>%{marker.size:.0f} size proxy<extra></extra>",
                name="zones",
            )
        )

    fig.update_layout(
        title=title,
        height=700,
        geo=dict(
            scope="europe",
            projection=dict(type="natural earth"),
            showland=True,
            landcolor="#f3f4f6",
            countrycolor="#d1d5db",
            coastlinecolor="#9ca3af",
            lonaxis=dict(range=[-25, 15]),
            lataxis=dict(range=[25, 45]),
        ),
        margin=dict(l=0, r=0, t=60, b=0),
    )
    return fig


def make_flow_dashboard(
    map_fig: go.Figure,
    detail_fig: go.Figure,
    *,
    layout: LayoutMode = "dual",
    title: str = "",
    map_subtitle: str = "Zone flow map",
    detail_subtitle: str = "Product / hub detail",
) -> go.Figure | tuple[go.Figure, go.Figure]:
    """Combine map + Sankey side-by-side, or return separate figures.

    Dual mode only copies Scattergeo + Sankey traces. Legend dummy Scatter traces
    must not be merged into geo/sankey subplots (Plotly renders them as garbage).
    """
    if layout == "separate":
        if title:
            map_fig.update_layout(title=f"{title} — {map_subtitle}")
            detail_fig.update_layout(title=f"{title} — {detail_subtitle}")
        return map_fig, detail_fig

    fig = make_subplots(
        rows=1,
        cols=2,
        column_widths=[0.58, 0.42],
        specs=[[{"type": "geo"}, {"type": "sankey"}]],
        subplot_titles=(map_subtitle, detail_subtitle),
    )
    for trace in map_fig.data:
        if isinstance(trace, go.Scattergeo):
            fig.add_trace(trace, row=1, col=1)
    for trace in detail_fig.data:
        if isinstance(trace, go.Sankey):
            fig.add_trace(trace, row=1, col=2)

    fig.update_layout(
        title=title,
        height=720,
        margin=dict(l=10, r=10, t=80, b=10),
    )
    if map_fig.layout.geo:
        fig.update_geos(map_fig.layout.geo, row=1, col=1)
    return fig


def hub_dashboard(
    flows: pd.DataFrame,
    hub: HubSpec,
    *,
    entity_map: pd.DataFrame,
    zone_coords: pd.DataFrame,
    focus_country: str,
    product: str | None = None,
    counterparty_grain: Grain = "zone",
    top_n: int = 12,
    min_kb: float = 500.0,
    map_top_edges: int = 20,
    layout: LayoutMode = "separate",
    products: list[str] | None = None,
    cargo_grain: CargoGrain = "product",
) -> go.Figure | tuple[go.Figure, go.Figure]:
    """One-call hub view: zone flow map + product detail Sankey."""
    prod_filter = [product] if product else products
    cargo_label = CARGO_LABEL[cargo_grain]
    inbound = build_hub_trade_edges(
        flows,
        hub,
        direction="inbound",
        counterparty_grain=counterparty_grain,
        entity_map=entity_map,
        products=prod_filter,
        top_n=top_n,
        min_kb=min_kb,
    )
    outbound = build_hub_trade_edges(
        flows,
        hub,
        direction="outbound",
        counterparty_grain=counterparty_grain,
        entity_map=entity_map,
        products=prod_filter,
        top_n=top_n,
        min_kb=min_kb,
    )

    hub_edges = flows_touching_hub(flows, hub)
    zone_edges = aggregate_zone_edges(
        hub_edges,
        focus_country=focus_country,
        product=product,
        min_kb=min_kb,
        top_n=map_top_edges,
    )
    highlight = {hub.zone} if hub.zone else set()
    if hub.grain == "zone" and hub.zone:
        highlight = {hub.zone}

    map_fig = plot_zone_flow_map(
        zone_edges,
        zone_coords,
        focus_country=focus_country,
        title="",
        highlight_zones=highlight,
        cargo_grain=cargo_grain,
    )

    if product:
        detail_fig = plot_product_flow_sankey(
            flows,
            product=product,
            hub=hub,
            display_grain="zone",
            top_n=map_top_edges,
            min_kb=min_kb,
            title="",
        )
    else:
        detail_fig = plot_hub_trade_sankey(
            hub.label,
            inbound,
            outbound,
            title="",
            cargo_grain=cargo_grain,
        )

    title = f"{focus_country} · {hub.label} ({hub.grain})"
    if product:
        title += f" · {product}"
    return make_flow_dashboard(
        map_fig,
        detail_fig,
        layout=layout,
        title=title,
        map_subtitle="Flows touching hub (zone arcs)",
        detail_subtitle=f"{cargo_label if product else 'Hub'} detail",
    )
