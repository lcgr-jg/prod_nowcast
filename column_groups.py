"""
Column groupings for Kpler Liquids trades (`columns=all`).

Primary taxonomy from desk notes (`notes_on_columns_grouping.docx`).
`EXTENSION_*` groups cover fields present in the 2026 dump but not listed
in those notes — kept separate so we don't silently rewrite the desk map.
"""

from __future__ import annotations

# --- Desk notes (source of truth) -------------------------------------------

PRINCIPAL_VESSEL_DATA = [
    "Vessel",
    "Id (vessel)",
    "IMO (vessel)",
    "MMSI (vessel)",
    "Vessel type",
    "Cargo type (vessel)",
    "Capacity (vessel m3)",
]

LOAD_PORT = [
    "Continent Origin",
    "TradingRegion (origin)",
    "Subcontinent (origin)",
    "Zone Origin",
    "Country (origin)",
    "Origin",
    "Installation origin",
    "Zone Origin Id",
    "Installation origin id",
    "Origin PortCall Id",
    "Eta source (origin)",
]

DESTINATION_PORT = [
    "Continent Destination",
    "TradingRegion (destination)",
    "Subcontinent (destination)",
    "Zone Destination",
    "Country (destination)",
    "Destination",
    "Installation Destination",
    "Zone Destination Id",
    "Installation Destination Id",
    "Destination PortCall Id",
    # Notes place forecast fields with destination geography
    "Forecasted destination",
    "Forecasted ETA",
    "Forecasted confidence",
    "Forecasted origin",
    "Forecasted origin ETA",
    "Forecasted origin confidence",
]

DATES = [
    "End (origin)",
    "Date (origin)",
    "Start (origin)",
    "Start (destination)",
    "Date (destination)",
    "End (destination)",
    "Eta (origin)",
    "Eta (destination)",
    "Kpler Date (origin)",
    "Kpler Date (destination)",
]

CARGO = [
    "Volume (bbl)",
    "Cargo (tons)",
    "Family",
    "Group",
    "Product",  # notes say "Products"; API header is "Product"
    "Id (Product)",
    "Grade",
    "Trade status",
    "Grade API",
    "Grade Sulfur",
    "Cargo Sources",
    "Estimated Product - Confidence",
    "Estimated products",
    "Estimated Products - Confidences",
]

VOYAGE_DATA = [
    "Id (Trade)",
    "Id (Voyage)",
    "Mileage",
    "Ton Miles",
    "Zone Canal Transit",
]

STS = [
    "Reload STS Partial (origin)",
    "Reload STS Partial (destination)",
    "Zone STS 1",
    "Zone STS 2",
    "Zone STS Id 1",
    "Zone STS Id 2",
    "Date Start STS 1",
    "Date Start STS 2",
    "Date End STS 1",
    "Date End STS 2",
    "Country STS 1",
    "Country STS 2",
    "Installation STS 1",
    "Installation STS 2",
    "Volume STS 1",
    "Volume STS 2",
    "Zone STS 3",
    "Zone STS Id 3",
    "Date Start STS 3",
    "Date End STS 3",
    "Country STS 3",
    "Installation STS 3",
    "Volume STS 3",
]

TRADING_DATA = [
    "Seller (origin)",
    "Buyer (destination)",
    "Import price",
    "Number of trades (import)",
    "Export price",
    "Number of trades (export)",
    "Charterer",
    "Bill Number",
    "Shipper",
    "Consignee",
    "Notify Party",
]

VOYAGE_LINKS = [
    "Intermediaries",  # notes: count of intermediaries
]

DESK_GROUPS: dict[str, list[str]] = {
    "Principal vessel data": PRINCIPAL_VESSEL_DATA,
    "Load Port": LOAD_PORT,
    "Destination Port": DESTINATION_PORT,
    "Dates": DATES,
    "Cargo": CARGO,
    "Voyage data": VOYAGE_DATA,
    "STS": STS,
    "Trading data": TRADING_DATA,
    "Voyage links": VOYAGE_LINKS,
}

# --- Present in dump, not in desk notes -------------------------------------

EXTENSION_VESSEL_EXTRA = [
    "Vessel DWT",
    "Vessel Owner",
    "Vessel Type Alternative ",  # trailing space is in the API header
]

EXTENSION_VESSEL_SECONDARY = [
    "Vessel Id 2",
    "Vessel Id 3",
    "Vessel Id 4",
    "Vessel Name 2",
    "Vessel Name 3",
    "Vessel Name 4",
    "Vessel IMO 2",
    "Vessel IMO 3",
    "Vessel IMO 4",
    "Vessel DWT 2",
    "Vessel DWT 3",
    "Vessel DWT 4",
    "Vessel Type 2",
    "Vessel Type 3",
    "Vessel Type 4",
    "Vessel Type Alternative 2",
    "Vessel Type Alternative 3",
    "Vessel Type Alternative 4",
    "Vessel Owner 2",
    "Vessel Owner 3",
    "Vessel Owner 4",
    "Voyage Id 2",
    "Voyage Id 3",
    "Voyage Id 4",
]

EXTENSION_DESTINATION_FORECAST = [
    "Forecasted origins",
    "Forecasted origins confidence",
    "Forecasted destinations",
    "Forecasted destinations confidence",
]

EXTENSION_LOAD_DEST_SYMMETRY = [
    "Eta source (destination)",  # notes only listed origin ETA source
]

EXTENSION_SHIPMENT_IDS = [
    "Id (Shipment)",
    "Parent Id (Shipment)",
]

EXTENSION_TRADE_LINKS = [
    "Link1 delivery",
    "Link1 type",
    "Link1 seller name",
    "Link1 seller country",
    "Link1 buyer name",
    "Link1 buyer country",
    "Link2 delivery",
    "Link2 type",
    "Link2 seller name",
    "Link2 seller country",
    "Link2 buyer name",
    "Link2 buyer country",
    "Link3 delivery",
    "Link3 type",
    "Link3 seller name",
    "Link3 seller country",
    "Link3 buyer name",
    "Link3 buyer country",
    "Link4 delivery",
    "Link4 type",
    "Link4 seller name",
    "Link4 seller country",
    "Link4 buyer name",
    "Link4 buyer country",
    "Link5 delivery",
    "Link5 type",
    "Link5 seller name",
    "Link5 seller country",
    "Link5 buyer name",
    "Link5 buyer country",
]

EXTENSION_GROUPS: dict[str, list[str]] = {
    "Extension — vessel extras": EXTENSION_VESSEL_EXTRA,
    "Extension — multi-vessel / secondary": EXTENSION_VESSEL_SECONDARY,
    "Extension — forecast (plural)": EXTENSION_DESTINATION_FORECAST,
    "Extension — dest ETA source": EXTENSION_LOAD_DEST_SYMMETRY,
    "Extension — shipment ids": EXTENSION_SHIPMENT_IDS,
    "Extension — Link1–5 chain": EXTENSION_TRADE_LINKS,
}

ALL_GROUPS: dict[str, list[str]] = {**DESK_GROUPS, **EXTENSION_GROUPS}

# Family → Group → Product hierarchy notes (from the same docx).
# Percents are desk estimates of how mass sits under each node, not API fills.
PRODUCT_HIERARCHY_NOTES = {
    "Clean": {
        "groups": {"(blank)": {"products": {"(blank)": "mostly blank grades"}}},
    },
    "Dirty": {
        "groups": {
            "Crude/Co": {
                "products": {
                    "Crude": "grade often blank or filled",
                    "Condensate": "grade often blank or filled",
                }
            },
            "(blank)": {"products": {"(blank)": None}},
        }
    },
    "DPP": {
        "groups": {
            "Fuel Oils": {"products": {"(various)": None}},
            "Resids": {
                "products": {
                    "Bitumen/Asphalt": "~80% grade blank",
                    "Coal Tar": "grade blank",
                    "Pitch": "grade blank",
                    "Specialities": "grade blank",
                }
            },
        }
    },
    "Light Ends": {
        "groups": {
            "Gasoline/Naphtha (~90%)": {
                "products": {
                    "Gasoline (~70%)": "~80% grade blank / 20% filled",
                    "Naphtha (~25%)": "~80% grade blank / 20% filled",
                    "(blank ~5%)": None,
                }
            },
            "Blendings (~9%)": {
                "products": {
                    "Clean Condensate (~20%)": "often condensate that belongs under Crude/Co",
                    "Blending Comps (~80%)": "mostly filled grades",
                }
            },
            "(blank ~1%)": {"products": {"(blank)": None}},
        }
    },
    "Middle Distillates": {
        "groups": {
            "Gasoil/Diesel (~77%)": {
                "products": {
                    "Diesel (~50%)": None,
                    "Gasoil (~40%)": "~90% grade blank",
                    "(blank ~10%)": None,
                }
            },
            "Kero/Jet": {
                "products": {
                    "Jet (~20% of MD rows in desk note)": "~80% have grade",
                    "Kerosene (~9%)": "mostly blank; some ULSK",
                }
            },
            "(blank ~1–3%)": {
                "products": {
                    "Cycle Oil (~33% of that pocket)": "LCO grades",
                    "(blank ~66%)": None,
                }
            },
        }
    },
}


def unassigned_columns(all_columns: list[str]) -> list[str]:
    known = {c for cols in ALL_GROUPS.values() for c in cols}
    return [c for c in all_columns if c not in known]
