"""
Enrich the Tableau trades master with desk tags from terminal_desk_map.csv.

Join order (coalesce):
  1. Installation == installation_name  (named desk rows)
  2. Else Country+Zone against desk rows with blank installation_name
     (zone-level stubs — treated as zone defaults)

Adds primary_tag, tag_*, owner/notes/complex/lat/lon, and tag_source
(installation | zone | blank).

Usage (from prod_nowcast/):
    python build_tableau_tagged_trades.py

Re-run after updating data/manual/terminal_desk_map.csv to refresh the
teammate extract.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from kpler_store import DATA_DIR
from terminal_desk import (
    DESK_MAP_PATH,
    _blank,
    is_tag_true,
    load_desk_map,
    tag_columns,
)

OUT_DIR = DATA_DIR / "derived" / "tableau"
TRADES_PATH = OUT_DIR / "trades_imp_exp_master.csv"
TAGGED_CSV_PATH = OUT_DIR / "trades_imp_exp_master_tagged.csv"
TAGGED_META_PATH = OUT_DIR / "trades_imp_exp_master_tagged_meta.json"

# Desk fields attached to each trade (beyond primary_tag / tag_*).
DESK_EXTRA_COLS = [
    "owner",
    "notes",
    "complex_id",
    "complex_name",
    "lat",
    "lon",
]


def _desk_keep_cols(desk: pd.DataFrame) -> list[str]:
    cols = ["primary_tag"] + tag_columns(desk)
    cols += [c for c in DESK_EXTRA_COLS if c in desk.columns]
    return cols


def _row_is_tagged(df: pd.DataFrame, prefix: str, keep_cols: list[str]) -> pd.Series:
    """True when primary_tag is set or any tag_* flag is TRUE under the given prefix."""
    primary = df.get(f"{prefix}primary_tag")
    if primary is None:
        hit = pd.Series(False, index=df.index)
    else:
        hit = ~_blank(primary)
    for col in keep_cols:
        if not col.startswith("tag_"):
            continue
        series = df.get(f"{prefix}{col}")
        if series is not None:
            hit = hit | series.map(is_tag_true)
    return hit


def _dedupe_prefer_tagged(
    frame: pd.DataFrame, subset: list[str], keep_cols: list[str]
) -> pd.DataFrame:
    # Prefer the row that already carries a primary_tag / tag_* so duplicates
    # do not randomly drop the desk annotation.
    tmp = frame.copy()
    tmp["_has_tag"] = (~_blank(tmp["primary_tag"])) | tmp[tag_columns(tmp)].apply(
        lambda col: col.map(is_tag_true)
    ).any(axis=1)
    return (
        tmp.sort_values("_has_tag", ascending=False)
        .drop_duplicates(subset=subset, keep="first")
        .drop(columns="_has_tag")
    )


def split_desk_maps(
    desk: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    keep_cols = _desk_keep_cols(desk)
    blank_install = _blank(desk["installation_name"])

    named = desk.loc[~blank_install].copy()
    stubs = desk.loc[blank_install].copy()

    named = _dedupe_prefer_tagged(named, ["installation_name"], keep_cols)
    stubs = _dedupe_prefer_tagged(stubs, ["country", "zone"], keep_cols)

    inst_map = named[["installation_name"] + keep_cols].rename(
        columns={c: f"_inst_{c}" for c in keep_cols}
    )
    zone_map = stubs[["country", "zone"] + keep_cols].rename(
        columns={c: f"_zone_{c}" for c in keep_cols}
    )
    return inst_map, zone_map, keep_cols


def enrich_trades(
    trades: pd.DataFrame,
    inst_map: pd.DataFrame,
    zone_map: pd.DataFrame,
    keep_cols: list[str],
) -> pd.DataFrame:
    n0 = len(trades)
    out = trades.merge(
        inst_map, how="left", left_on="Installation", right_on="installation_name"
    )
    out = out.drop(columns=["installation_name"], errors="ignore")

    out = out.merge(
        zone_map,
        how="left",
        left_on=["Country", "Zone"],
        right_on=["country", "zone"],
    )
    out = out.drop(columns=[c for c in ("country", "zone") if c in out.columns])

    has_inst = _row_is_tagged(out, "_inst_", keep_cols)
    has_zone = _row_is_tagged(out, "_zone_", keep_cols)

    for col in keep_cols:
        ic, zc = f"_inst_{col}", f"_zone_{col}"
        if ic in out.columns and zc in out.columns:
            # Install-level tags win; zone stubs only fill gaps.
            out[col] = out[ic].where(has_inst, out[zc])
        elif ic in out.columns:
            out[col] = out[ic]
        elif zc in out.columns:
            out[col] = out[zc]

    out["tag_source"] = pd.NA
    out.loc[has_inst, "tag_source"] = "installation"
    out.loc[~has_inst & has_zone, "tag_source"] = "zone"

    drop_tmp = [c for c in out.columns if c.startswith("_inst_") or c.startswith("_zone_")]
    out = out.drop(columns=drop_tmp)

    if len(out) != n0:
        raise RuntimeError(f"Row count changed after merge: {n0} -> {len(out)}")
    return out


def build(
    *,
    trades_path: Path = TRADES_PATH,
    desk_path: Path = DESK_MAP_PATH,
    out_path: Path = TAGGED_CSV_PATH,
    meta_path: Path = TAGGED_META_PATH,
) -> dict:
    if not trades_path.exists():
        raise FileNotFoundError(
            f"Missing {trades_path}. Run build_tableau_imp_exp.py first."
        )

    desk = load_desk_map(desk_path)
    inst_map, zone_map, keep_cols = split_desk_maps(desk)

    trades = pd.read_csv(trades_path, low_memory=False)
    tagged = enrich_trades(trades, inst_map, zone_map, keep_cols)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tagged.to_csv(out_path, index=False)

    source_counts = (
        tagged["tag_source"].value_counts(dropna=False).rename(index={pd.NA: None})
    )
    # JSON-friendly counts (None key for untagged)
    tag_source = {
        ("untagged" if k is None or (isinstance(k, float) and pd.isna(k)) else str(k)): int(v)
        for k, v in source_counts.items()
    }

    meta = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "rows_total": int(len(tagged)),
        "tag_source": tag_source,
        "primary_tag_non_null": int(tagged["primary_tag"].notna().sum())
        if "primary_tag" in tagged.columns
        else 0,
        "named_installs_in_map": int(len(inst_map)),
        "zone_stubs_in_map": int(len(zone_map)),
        "desk_columns_added": keep_cols + ["tag_source"],
        "join": {
            "primary": "Installation == installation_name",
            "fallback": "Country+Zone vs blank installation_name stubs",
        },
        "paths": {
            "trades": str(trades_path),
            "desk_map": str(desk_path),
            "csv": str(out_path),
        },
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trades",
        type=Path,
        default=TRADES_PATH,
        help="Tableau trades master CSV (default: derived/tableau/trades_imp_exp_master.csv)",
    )
    parser.add_argument(
        "--desk",
        type=Path,
        default=DESK_MAP_PATH,
        help="Desk map CSV (default: manual/terminal_desk_map.csv)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=TAGGED_CSV_PATH,
        help="Output CSV path",
    )
    args = parser.parse_args()
    meta = build(trades_path=args.trades, desk_path=args.desk, out_path=args.out)
    print(json.dumps(meta, indent=2))
    print("done")


if __name__ == "__main__":
    main()
