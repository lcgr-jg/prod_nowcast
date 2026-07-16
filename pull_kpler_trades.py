"""
Download global Kpler Liquids trades (columns=all) year by year.

Mirrors the desk's proven REST dump pattern (Bearer login + /v1/trades),
with credentials from .env and safer defaults for large pulls.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

BASE_URL = "https://api.kpler.com/v1"
HISTORY_START_YEAR = 2016
# Year dumps can be large; avoid the default requests timeout cutting them off.
REQUEST_TIMEOUT_S = 60 * 60

ROOT = Path(__file__).resolve().parent
SAVE_FOLDER = ROOT / "Raw Data" / "Kpler"
ARCHIVE_FOLDER = SAVE_FOLDER / "Archive"


def load_credentials() -> tuple[str, str]:
    # Local .env wins over shell exports so desk machines stay predictable.
    load_dotenv(ROOT / ".env", override=True)
    email = os.getenv("KPLER_EMAIL")
    password = os.getenv("KPLER_PASSWORD")
    if not email or not password:
        print(
            "Missing KPLER_EMAIL / KPLER_PASSWORD. "
            "Copy .env.example to .env and fill in your credentials.",
            file=sys.stderr,
        )
        sys.exit(1)
    return email, password


def get_token(email: str, password: str) -> str:
    response = requests.post(
        f"{BASE_URL}/login",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"email": email, "password": password}),
        timeout=60,
    )
    response.raise_for_status()
    token = response.json().get("token")
    if not token:
        raise RuntimeError(f"Login succeeded but no token in response: {response.text[:200]}")
    return token


def years_to_download(
    download_previous: bool,
    last_year: int,
    explicit_years: list[int] | None = None,
) -> list[int]:
    if explicit_years:
        return sorted(explicit_years)
    if download_previous:
        return list(range(HISTORY_START_YEAR, last_year + 1))
    return [last_year]


def trades_url(year: int, open_ended_year: int) -> str:
    # Only the true calendar open-ended year omits endDate (live YTD dump).
    if year == open_ended_year == date.today().year:
        return f"{BASE_URL}/trades?startDate={year}-01-01&columns=all"
    return f"{BASE_URL}/trades?startDate={year}-01-01&endDate={year}-12-31&columns=all"


def year_csv_path(year: int) -> Path:
    return SAVE_FOLDER / f"kpler_{year}.csv"


def download_year(token: str, year: int, open_ended_year: int, *, force: bool) -> Path:
    out_path = year_csv_path(year)
    if out_path.exists() and not force:
        print(f"{year}: skip (already exists: {out_path})")
        return out_path

    SAVE_FOLDER.mkdir(parents=True, exist_ok=True)
    url = trades_url(year, open_ended_year)
    print(f"{year}: downloading {url}")

    # Stream to disk — a global year with columns=all should not sit in memory as text.
    tmp_path = out_path.with_suffix(".csv.partial")
    with requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        stream=True,
        timeout=REQUEST_TIMEOUT_S,
    ) as response:
        response.raise_for_status()
        with tmp_path.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)

    tmp_path.replace(out_path)
    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"{year}: saved {out_path} ({size_mb:.1f} MB)")
    return out_path


def peek_row_count(path: Path) -> int | None:
    """Cheap sanity check: count lines without loading the full frame."""
    try:
        with path.open("rb") as fh:
            # Subtract header; Kpler trades CSV is semicolon-delimited.
            return max(sum(1 for _ in fh) - 1, 0)
    except OSError:
        return None


def concat_available_years(last_year: int) -> Path | None:
    paths = [year_csv_path(y) for y in range(HISTORY_START_YEAR, last_year + 1) if year_csv_path(y).exists()]
    if not paths:
        print("No yearly CSVs found to concatenate.")
        return None

    print(f"Concatenating {len(paths)} file(s)...")
    frames = []
    for path in paths:
        # Raw Kpler dump uses ';' — keep that contract when reading back.
        frames.append(pd.read_csv(path, sep=";", low_memory=False))

    combined = pd.concat(frames, ignore_index=True)
    SAVE_FOLDER.mkdir(parents=True, exist_ok=True)
    ARCHIVE_FOLDER.mkdir(parents=True, exist_ok=True)

    out_path = SAVE_FOLDER / "kpler_trades.csv"
    archive_path = ARCHIVE_FOLDER / f"{date.today().isoformat()}_kpler_trades.csv"
    combined.to_csv(out_path, index=False)
    combined.to_csv(archive_path, index=False)
    print(f"Wrote {out_path} and {archive_path} ({len(combined):,} rows)")
    return out_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pull global Kpler trades dumps by year.")
    parser.add_argument(
        "--previous-years",
        action="store_true",
        help=f"Download {HISTORY_START_YEAR} through current last year (default: last year only).",
    )
    parser.add_argument(
        "--last-year",
        type=int,
        default=date.today().year,
        help="Latest year to include (default: calendar year).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if the yearly CSV already exists.",
    )
    parser.add_argument(
        "--years",
        type=int,
        nargs="+",
        default=None,
        help="Explicit year list to download (e.g. --years 2024 2025).",
    )
    parser.add_argument(
        "--concat",
        action="store_true",
        help="After download, concat any available yearly CSVs into kpler_trades.csv.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    email, password = load_credentials()
    token = get_token(email, password)
    print("Authenticated with Kpler.")

    years = years_to_download(args.previous_years, args.last_year, args.years)
    open_ended_year = date.today().year
    for year in years:
        path = download_year(token, year, open_ended_year, force=args.force)
        n = peek_row_count(path)
        if n is not None:
            print(f"{year}: ~{n:,} data rows")

    if args.concat:
        concat_available_years(args.last_year)
    elif not args.previous_years:
        print("Tip: re-run with --previous-years once 2026 looks good; use --concat when ready to merge.")

    print("done")


if __name__ == "__main__":
    main()
