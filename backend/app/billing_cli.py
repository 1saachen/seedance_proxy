"""Manage Richi Proxy billing versions from the server."""

import argparse
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
import sys

from . import billing, statements
from .config import Settings
from .db import Database


def _effective_at(value: str) -> int:
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("use an ISO 8601 time with a timezone and whole seconds") from None
    if timestamp.utcoffset() is None or timestamp.microsecond:
        raise argparse.ArgumentTypeError("use an ISO 8601 time with a timezone and whole seconds")
    return int(timestamp.timestamp())


def _yuan_per_million(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,6})?", value):
        raise argparse.ArgumentTypeError("use a nonnegative yuan amount with at most six decimal places")
    return int(Decimal(value).scaleb(6))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, help="initialized SQLite path (defaults to SEEDANCE_DATABASE_PATH)")
    actions = parser.add_subparsers(dest="action", required=True)

    price = actions.add_parser("add-price", help="append a rate version")
    price.add_argument("--upstream-model", required=True)
    price.add_argument("--resolution", required=True, choices=("480p", "720p", "1080p"))
    price.add_argument("--video-input", required=True, choices=("yes", "no"))
    price.add_argument("--effective-at", required=True, type=_effective_at)
    price.add_argument("--yuan-per-million", required=True, type=_yuan_per_million)
    price.add_argument("--source", required=True, help="source for the verified price or project contract")

    terms = actions.add_parser("set-project-terms", help="append effective project billing terms")
    terms.add_argument("--project-id", required=True)
    terms.add_argument("--effective-at", required=True, type=_effective_at)
    terms.add_argument("--payable-bps", type=int, default=10000, help="8000 means the project pays 80 percent")
    enabled = terms.add_mutually_exclusive_group(required=True)
    enabled.add_argument("--enabled", dest="enabled", action="store_true")
    enabled.add_argument("--disabled", dest="enabled", action="store_false")

    actions.add_parser("reconcile", help="replay unrecorded or pending successful calls")
    confirm = actions.add_parser("confirm-statement", help="finalize one past customer billing month")
    confirm.add_argument("--project-id", required=True)
    confirm.add_argument("--month", required=True, help="Beijing billing month in YYYY-MM")
    return parser


def _run(args: argparse.Namespace) -> dict[str, int | str]:
    path = args.database if args.database is not None else Settings().database_path
    if not path.is_file():
        raise ValueError("SQLite database does not exist. Initialize it with the Richi Proxy server first.")
    database = Database(path)
    if args.action == "reconcile":
        after = processed = 0
        while True:
            with database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                after, batch_size = billing.reconcile_batch(connection, after_rowid=after)
            if not batch_size:
                return {"processedTasks": processed}
            processed += batch_size

    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if args.action == "confirm-statement":
            statement = statements.confirm_statement(connection, args.project_id, args.month)
            return {
                "statementId": statement["id"], "month": statement["month"],
                "status": statement["status"], "payableAmount": statement["summary"]["payableAmount"],
            }
        if args.action == "add-price":
            version = billing.add_price(
                connection, upstream_model=args.upstream_model, resolution=args.resolution,
                contains_video_input=args.video_input == "yes", effective_at=args.effective_at,
                unit_price_micros_per_million=args.yuan_per_million, source=args.source,
            )
            return {"priceVersion": version}
        version = billing.add_project_terms(
            connection, project_id=args.project_id, effective_at=args.effective_at,
            enabled=args.enabled, payable_bps=args.payable_bps,
        )
        return {"termsVersion": version}


def main() -> int:
    args = _parser().parse_args()
    try:
        print(json.dumps(_run(args)))
    except statements.StatementError as error:
        print(f"Billing command failed ({error.code}): {error}", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"Billing command failed: {error}", file=sys.stderr)
        return 1
    except sqlite3.Error:
        print("Billing command failed: could not write to the initialized SQLite database", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
