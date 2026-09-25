"""The command line. `consult init` applies the schema."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from consult import config, store
from consult.config import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="consult")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="apply schema.sql to the configured database")
    return parser


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    args = build_parser().parse_args(argv)
    resolved = config.load() if settings is None else settings
    if args.command == "init":
        with store.connect(resolved) as conn:
            store.init(conn)
        # The database name and nothing else: no host, no user, no password.
        print(f"schema applied: {resolved.db_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
