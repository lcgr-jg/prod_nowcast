"""Bootstrap DuckDB trades table from yearly Raw Data/Kpler/kpler_YYYY.csv dumps."""

from __future__ import annotations

import argparse
import sys

from kpler_store import DB_PATH, bootstrap_from_yearly_csvs, table_stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--append",
        action="store_true",
        help="Do not drop existing trades (default rebuilds the table).",
    )
    args = parser.parse_args()

    print(f"Writing {DB_PATH}")
    try:
        summary = bootstrap_from_yearly_csvs(replace=not args.append)
    except Exception as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        raise

    print(
        f"Loaded {summary['files']} files -> {summary['rows']:,} rows "
        f"({summary['distinct_trades']:,} distinct trade ids)"
    )
    stats = table_stats()
    print(
        f"Date (origin) range: {stats['date_origin_min']} -> {stats['date_origin_max']}"
    )
    print("done")


if __name__ == "__main__":
    main()
