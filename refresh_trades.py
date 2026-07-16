"""
Incremental Kpler trades refresh into DuckDB.

Pulls a rolling window (default: last 180 days) via the same REST dump pattern,
then upserts by (Id (Trade), Id (Product)).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
import os

from kpler_store import DATA_DIR, DB_PATH, upsert_csv

BASE_URL = "https://api.kpler.com/v1"
REQUEST_TIMEOUT_S = 60 * 60
ROOT = Path(__file__).resolve().parent
INCREMENTAL_DIR = DATA_DIR / "incremental"


def load_credentials() -> tuple[str, str]:
    load_dotenv(ROOT / ".env", override=True)
    email = os.getenv("KPLER_EMAIL")
    password = os.getenv("KPLER_PASSWORD")
    if not email or not password:
        print("Missing KPLER_EMAIL / KPLER_PASSWORD in .env", file=sys.stderr)
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
        raise RuntimeError("Login ok but no token returned")
    return token


def download_window(token: str, start: date, end: date | None, out_path: Path) -> Path:
    if end is None:
        url = f"{BASE_URL}/trades?startDate={start.isoformat()}&columns=all"
    else:
        url = (
            f"{BASE_URL}/trades?startDate={start.isoformat()}"
            f"&endDate={end.isoformat()}&columns=all"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".partial")
    print(f"Downloading {url}")
    with requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        stream=True,
        timeout=REQUEST_TIMEOUT_S,
    ) as response:
        response.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)
    tmp.replace(out_path)
    print(f"Saved {out_path} ({out_path.stat().st_size / 1e6:.1f} MB)")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=180,
        help="Rolling lookback window in days (default 180).",
    )
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=None,
        help="Override window start (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=None,
        help="Optional window end (YYYY-MM-DD). Default: open-ended.",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Skip download; upsert this CSV instead.",
    )
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(
            f"Missing {DB_PATH}. Run: python bootstrap_duckdb.py",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.csv is not None:
        csv_path = args.csv
    else:
        start = args.start or (date.today() - timedelta(days=args.days))
        end = args.end
        stamp = date.today().isoformat()
        csv_path = INCREMENTAL_DIR / f"trades_{stamp}_{start.isoformat()}.csv"
        email, password = load_credentials()
        token = get_token(email, password)
        download_window(token, start, end, csv_path)

    result = upsert_csv(csv_path)
    print(
        f"Upserted {result['staged_rows']:,} staged rows → "
        f"table now {result['table_rows']:,} rows"
    )
    print("done")


if __name__ == "__main__":
    main()
