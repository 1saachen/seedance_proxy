import argparse
import getpass
import sqlite3
import sys
import warnings
from pathlib import Path

from pydantic import SecretStr

from . import admin_auth
from .db import Database


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage administrator accounts on a local RichProxy database")
    parser.add_argument("--database", type=Path, required=True, help="path to an initialized RichProxy SQLite database")
    actions = parser.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create-admin", help="create an administrator using a hidden password prompt")
    create.add_argument("username")
    return parser


def _prompt_password() -> SecretStr:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        password = getpass.getpass("Administrator password: ")
        confirmation = getpass.getpass("Repeat administrator password: ")
    if password != confirmation:
        raise ValueError("Passwords do not match")
    return SecretStr(password)


def main() -> int:
    args = _parser().parse_args()
    if not args.database.is_file():
        print("Start RichProxy to initialize the specified database before creating an administrator.", file=sys.stderr)
        return 1
    database = Database(args.database)
    try:
        with database.connect() as connection:
            projects_table = connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'projects'",
            ).fetchone()
            if projects_table is None:
                raise ValueError("The specified file is not an initialized RichProxy database")
        password = _prompt_password()
        with database.connect() as connection:
            admin_auth.init_schema(connection)
            admin = admin_auth.create_admin(connection, args.username, password)
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print("Administrator creation requires an interactive terminal with hidden password entry.", file=sys.stderr)
        return 1
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    except sqlite3.Error:
        print("Unable to write the administrator account to the specified database.", file=sys.stderr)
        return 1
    print(f"Created administrator {admin.username}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
