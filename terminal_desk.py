"""
Desk-maintained terminal metadata (owners, primary tag, boolean tags, complexes).

Single source of truth: ``data/manual/terminal_desk_map.csv``.

Tag flags are columns named ``tag_*``. Blank = False. Add a new column to
grow the vocabulary; catalog upsert preserves any ``tag_*`` fields.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from kpler_store import DATA_DIR

MANUAL_DIR = DATA_DIR / "manual"
DESK_MAP_PATH = MANUAL_DIR / "terminal_desk_map.csv"

TAG_PREFIX = "tag_"

# Closed starter set — extra tag_* columns on the CSV are kept automatically.
STARTER_TAGS = [
    "refinery",
    "storage",
    "blending",
    "bunkering",
    "heavy_industry",
    "power_plant",
    "airport",
    "agriculture",
    "military",
    "biofuels",
    "asphalt",
]
STARTER_TAG_COLS = [f"{TAG_PREFIX}{t}" for t in STARTER_TAGS]

# Old exclusive facility_type → primary_tag
LEGACY_PRIMARY = {
    "port_storage": "storage",
    "refinery": "refinery",
    "bunkering": "bunkering",
    "blending": "blending",
    "heavy_industry": "heavy_industry",
    "agriculture": "agriculture",
    "asphalt_ref": "asphalt",
    "aiport_storage": "airport",
    "military_base": "military",
    "biofuels": "biofuels",
}

# Exclusive labels that were "other" but notes make the function clear.
OTHER_PRIMARY = {
    "ICL Fuentes Fertilizantes": "heavy_industry",
    "Ineos Bilbao": "heavy_industry",
}

# Extra flags beyond primary (membership, not exclusive).
LEGACY_EXTRA_TAGS = {
    "aiport_storage": ("airport", "storage"),
}

SCAFFOLD_COLS = [
    "terminal_key",
    "installation_id",
    "installation_name",
    "country",
    "zone",
    "zone_id",
]

DESK_BEFORE_TAGS = ["owner", "primary_tag"]
DESK_AFTER_TAGS = [
    "complex_id",
    "complex_name",
    "regions_covered",
    "pipeline_connected",
    "pipeline_names",
    "lat",
    "lon",
    "notes",
]

# Minimum required desk fields (tag_* discovered dynamically).
CORE_DESK_COLS = DESK_BEFORE_TAGS + DESK_AFTER_TAGS
DESK_COLS = CORE_DESK_COLS + STARTER_TAG_COLS
DESK_MAP_COLS = SCAFFOLD_COLS + DESK_COLS

LEGACY_OWNERS = DATA_DIR / "derived" / "installation_owners.csv"
LEGACY_COMPLEXES = DATA_DIR / "derived" / "terminal_complexes.csv"
LEGACY_ENRICHMENT = DATA_DIR / "derived" / "terminals_enrichment_template.csv"

_TRUE_TOKENS = {"true", "1", "1.0", "yes", "y", "t"}


def _norm_name(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.casefold()


def _blank(series: pd.Series) -> pd.Series:
    return series.isna() | series.astype(str).str.strip().eq("") | series.astype(str).str.strip().eq("nan")


def _combine_notes(*values) -> str:
    parts: list[str] = []
    for value in values:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            continue
        text = str(value).strip()
        if text and text not in parts:
            parts.append(text)
    return " | ".join(parts)


def is_tag_true(value) -> bool:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in _TRUE_TOKENS


def tag_columns(df: pd.DataFrame) -> list[str]:
    found = [c for c in df.columns if str(c).startswith(TAG_PREFIX)]
    starter = [c for c in STARTER_TAG_COLS if c in found]
    extra = [c for c in found if c not in STARTER_TAG_COLS]
    return starter + extra


def tag_name(col: str) -> str:
    return str(col)[len(TAG_PREFIX) :] if str(col).startswith(TAG_PREFIX) else str(col)


def desk_columns(df: pd.DataFrame) -> list[str]:
    tags = tag_columns(df)
    for col in STARTER_TAG_COLS:
        if col not in tags:
            tags.append(col)
    ordered = SCAFFOLD_COLS + DESK_BEFORE_TAGS + tags + DESK_AFTER_TAGS
    return [c for c in ordered if c in df.columns]


def ensure_tag_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in STARTER_TAG_COLS:
        if col not in out.columns:
            out[col] = pd.NA
    return out


def sites_with_tag(desk: pd.DataFrame, tag: str) -> pd.DataFrame:
    col = tag if str(tag).startswith(TAG_PREFIX) else f"{TAG_PREFIX}{tag}"
    if col not in desk.columns:
        return desk.iloc[0:0].copy()
    mask = desk[col].map(is_tag_true)
    return desk.loc[mask].copy()


def explode_tags(desk: pd.DataFrame) -> pd.DataFrame:
    """Long format: one row per (installation, tag) where the flag is TRUE."""
    rows: list[dict] = []
    for col in tag_columns(desk):
        name = tag_name(col)
        hit = desk.loc[desk[col].map(is_tag_true)]
        for rec in hit.itertuples(index=False):
            rows.append(
                {
                    "installation_name": rec.installation_name,
                    "tag": name,
                    "primary_tag": getattr(rec, "primary_tag", pd.NA),
                    "country": getattr(rec, "country", pd.NA),
                    "zone": getattr(rec, "zone", pd.NA),
                    "owner": getattr(rec, "owner", pd.NA),
                }
            )
    return pd.DataFrame(rows)


def serialize_tags(df: pd.DataFrame) -> pd.DataFrame:
    """TRUE for flagged tags, blank otherwise — easier to scan in Excel."""
    out = df.copy()
    for col in tag_columns(out):
        out[col] = out[col].map(lambda v: "TRUE" if is_tag_true(v) else "")
    return out


def empty_desk_frame(scaffold: pd.DataFrame) -> pd.DataFrame:
    out = scaffold[SCAFFOLD_COLS].copy()
    for col in CORE_DESK_COLS + STARTER_TAG_COLS:
        out[col] = pd.NA
    return out[desk_columns(out)]


def _read_desk_csv(path: Path, **kwargs) -> pd.DataFrame:
    """Excel on Windows often re-saves the desk map as Latin-1; prefer UTF-8."""
    errors: list[Exception] = []
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return pd.read_csv(path, encoding=encoding, **kwargs)
        except UnicodeDecodeError as exc:
            errors.append(exc)
    raise errors[-1]


def load_desk_map(path: Path | None = None) -> pd.DataFrame:
    path = Path(path or DESK_MAP_PATH)
    if not path.exists():
        raise FileNotFoundError(f"Missing desk map: {path}")
    # dtype keeps id cols as strings; encoding fallback handles Excel Latin-1 saves
    df = _read_desk_csv(
        path, dtype={"installation_id": "string", "zone_id": "string"}
    )
    if "facility_type" in df.columns and "primary_tag" not in df.columns:
        df = df.rename(columns={"facility_type": "primary_tag"})
    missing = [c for c in SCAFFOLD_COLS + CORE_DESK_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"{path.name} missing columns: {missing}")
    df["installation_name"] = df["installation_name"].astype(str).str.strip()
    df["terminal_key"] = df["terminal_key"].astype(str).str.strip()
    df = ensure_tag_columns(df)
    return df[desk_columns(df)].copy()


def owners_view(desk: pd.DataFrame) -> pd.DataFrame:
    """Legacy join shape for flow tagging (installation, owner, site_type)."""
    out = desk.copy()
    out["installation"] = out["installation_name"].astype(str).str.strip()
    out["owner"] = out["owner"].astype(str).str.strip().replace({"nan": "", "<NA>": ""})
    primary = out["primary_tag"] if "primary_tag" in out.columns else out.get("facility_type")
    out["site_type"] = primary.astype(str).str.strip().replace({"nan": "", "<NA>": ""})
    out.loc[out["owner"].eq(""), "owner"] = pd.NA
    out.loc[out["site_type"].eq(""), "site_type"] = pd.NA
    return out[["installation", "owner", "site_type"]].drop_duplicates("installation", keep="first")


def complex_map_view(desk: pd.DataFrame) -> pd.DataFrame:
    """Installation → complex columns for entity rollup."""
    mapped = desk.loc[~_blank(desk["complex_id"])].copy()
    mapped["Installation"] = mapped["installation_name"].astype(str).str.strip()
    return mapped[["Installation", "complex_id", "complex_name"]].drop_duplicates(
        "Installation", keep="first"
    )


def migrate_facility_type_to_tags(df: pd.DataFrame) -> pd.DataFrame:
    """Map exclusive facility_type / primary_tag values onto starter tag flags."""
    out = ensure_tag_columns(df)
    if "facility_type" in out.columns and "primary_tag" not in out.columns:
        out["primary_tag"] = out["facility_type"]
    legacy = out["primary_tag"].astype(str).str.strip()

    mapped = legacy.map(lambda v: LEGACY_PRIMARY.get(v, pd.NA) if v and v != "nan" else pd.NA)
    for inst, tag in OTHER_PRIMARY.items():
        mapped = mapped.where(out["installation_name"].ne(inst), tag)
    out["primary_tag"] = mapped

    for col in STARTER_TAG_COLS:
        out[col] = ""

    extras = legacy.map(lambda v: LEGACY_EXTRA_TAGS.get(v, ()))
    for i in out.index:
        flags: list[str] = []
        primary = out.at[i, "primary_tag"]
        if pd.notna(primary) and str(primary).strip() and str(primary).strip() != "nan":
            flags.append(str(primary).strip())
        flags.extend(extras.at[i] if i in extras.index else ())
        for tag in dict.fromkeys(flags):
            col = f"{TAG_PREFIX}{tag}"
            if col not in out.columns:
                out[col] = ""
            out.at[i, col] = "TRUE"

    if "facility_type" in out.columns:
        out = out.drop(columns=["facility_type"])
    return serialize_tags(ensure_tag_columns(out))[desk_columns(ensure_tag_columns(out))]


def merge_desk_sources(
    *,
    scaffold_path: Path | None = None,
    owners_path: Path | None = None,
    complexes_path: Path | None = None,
) -> pd.DataFrame:
    """Merge scaffold + legacy owner/complex CSVs into desk map shape."""
    scaffold_path = Path(scaffold_path or LEGACY_ENRICHMENT)
    owners_path = Path(owners_path or LEGACY_OWNERS)
    complexes_path = Path(complexes_path or LEGACY_COMPLEXES)

    if not scaffold_path.exists():
        raise FileNotFoundError(f"Missing scaffold: {scaffold_path}")

    desk = pd.read_csv(
        scaffold_path, dtype={"installation_id": "string", "zone_id": "string"}
    )
    desk = empty_desk_frame(desk)
    desk["installation_name"] = desk["installation_name"].astype(str).str.strip()
    desk["_join"] = _norm_name(desk["installation_name"])

    if owners_path.exists():
        owners = pd.read_csv(owners_path).rename(
            columns={"installation": "installation_name", "site_type": "primary_tag"}
        )
        owners["installation_name"] = owners["installation_name"].astype(str).str.strip()
        owners["_join"] = _norm_name(owners["installation_name"])
        owners = owners.drop_duplicates("_join", keep="first")[
            ["_join", "owner", "primary_tag", "notes"]
        ].rename(columns={"notes": "owner_notes"})
        desk = desk.drop(columns=["owner", "primary_tag"], errors="ignore")
        desk = desk.merge(owners, on="_join", how="left")
        desk["notes"] = [
            _combine_notes(n, o)
            for n, o in zip(desk["notes"], desk["owner_notes"], strict=True)
        ]
        desk = desk.drop(columns=["owner_notes"])

    if complexes_path.exists():
        complexes = pd.read_csv(complexes_path).rename(
            columns={"installation": "installation_name"}
        )
        complexes["installation_name"] = complexes["installation_name"].astype(str).str.strip()
        complexes["_join"] = _norm_name(complexes["installation_name"])
        complexes = complexes.drop_duplicates("_join", keep="first")[
            ["_join", "complex_id", "complex_name", "notes"]
        ].rename(columns={"notes": "complex_notes"})
        desk = desk.drop(columns=["complex_id", "complex_name"], errors="ignore")
        desk = desk.merge(complexes, on="_join", how="left")
        desk["notes"] = [
            _combine_notes(n, c)
            for n, c in zip(desk["notes"], desk["complex_notes"], strict=True)
        ]
        desk = desk.drop(columns=["complex_notes"])

    desk = desk.drop(columns=["_join"])
    return migrate_facility_type_to_tags(desk)


def upsert_desk_map(
    terminals: pd.DataFrame,
    *,
    path: Path | None = None,
) -> Path:
    """Refresh Kpler scaffold cols; preserve desk edits including any tag_* columns."""
    path = Path(path or DESK_MAP_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)

    incoming = empty_desk_frame(terminals)
    incoming["terminal_key"] = incoming["terminal_key"].astype(str).str.strip()

    if path.exists():
        existing = load_desk_map(path)
        for col in tag_columns(existing):
            if col not in incoming.columns:
                incoming[col] = pd.NA
        lookup = existing.set_index("terminal_key")
        preserve = [c for c in existing.columns if c not in SCAFFOLD_COLS]
        out = incoming.copy()
        for col in preserve:
            if col in lookup.columns:
                out[col] = out["terminal_key"].map(lookup[col])
        return write_desk_map(out, path=path)

    if LEGACY_OWNERS.exists() or LEGACY_COMPLEXES.exists():
        merged = merge_desk_sources()
        out = incoming.merge(
            merged,
            on="terminal_key",
            how="left",
            suffixes=("", "_legacy"),
        )
        for col in [c for c in out.columns if c.endswith("_legacy")]:
            base = col.removesuffix("_legacy")
            out[base] = out[col].combine_first(out[base]) if base in out.columns else out[col]
            out = out.drop(columns=[col])
        return write_desk_map(out, path=path)

    return write_desk_map(incoming, path=path)


def write_desk_map(df: pd.DataFrame, *, path: Path | None = None) -> Path:
    path = Path(path or DESK_MAP_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = serialize_tags(ensure_tag_columns(df))
    cols = desk_columns(out)
    # Always UTF-8 so notebook/script loads stay stable after Excel edits
    out[cols].to_csv(path, index=False, encoding="utf-8")
    return path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--merge-legacy",
        action="store_true",
        help="Merge legacy owners/complexes/enrichment CSVs into data/manual/terminal_desk_map.csv",
    )
    parser.add_argument(
        "--migrate-tags",
        action="store_true",
        help="Rewrite facility_type into primary_tag + tag_* boolean columns",
    )
    parser.add_argument(
        "--rewrite-utf8",
        action="store_true",
        help="Load desk map (any Excel encoding) and rewrite as UTF-8 in place",
    )
    args = parser.parse_args()
    if args.migrate_tags:
        raw = _read_desk_csv(
            DESK_MAP_PATH, dtype={"installation_id": "string", "zone_id": "string"}
        )
        path = write_desk_map(migrate_facility_type_to_tags(raw))
        print(f"migrated tags -> {path}")
    elif args.merge_legacy:
        path = write_desk_map(merge_desk_sources())
        print(f"wrote {path}")
    elif args.rewrite_utf8:
        path = write_desk_map(load_desk_map(DESK_MAP_PATH))
        print(f"rewrote UTF-8 -> {path}")
