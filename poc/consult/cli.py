"""The command line.

`consult init` applies the schema (`--reset` drops it first). `consult
validate` runs the validator over a responses file and its definition
workbook and prints the report: exit 0 with no errors, 1 when an error
blocks, 2 when the file was refused before it was read.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from consult import config, report, store
from consult.config import Settings
from consult.definition import DefinitionError, read_definition
from consult.inputs import InputError
from consult.responses import Responses
from consult.validate import validate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="consult")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="apply schema.sql to the configured database")
    init.add_argument(
        "--reset",
        action="store_true",
        help="drop every table and schema first; no rows survive",
    )
    check = commands.add_parser("validate", help="validate a responses file against its definition")
    check.add_argument("responses", type=Path, help="the responses file, CSV or XLSX")
    check.add_argument("--definition", type=Path, required=True, help="the definition workbook")
    check.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


def _validate(args: argparse.Namespace, settings: Settings) -> int:
    try:
        definition = read_definition(args.definition, settings.caps)
        responses = Responses(args.responses, settings.caps)
        result = validate(definition, responses, settings.rates)
    except InputError as exc:
        # A reason and a count, never the content (THREAT_MODEL.md, row 1).
        print(f"refused: {exc}")
        return 2
    except DefinitionError as exc:
        if args.json:
            print(json.dumps({"errors": list(exc.problems)}, indent=2))
        else:
            print(f"errors: {len(exc.problems)}")
            print("\n".join(f"  {problem}" for problem in exc.problems))
        return 1
    print(
        report.as_json(result)
        if args.json
        else report.render(result, args.responses, definition, args.definition)
    )
    return 1 if result.errors else 0


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    args = build_parser().parse_args(argv)
    resolved = config.load() if settings is None else settings
    if args.command == "init":
        with store.connect(resolved) as conn:
            if args.reset:
                store.reset(conn)
            else:
                store.init(conn)
        # The database name and nothing else: no host, no user, no password.
        print(f"schema {'reset' if args.reset else 'applied'}: {resolved.db_name}")
        return 0
    return _validate(args, resolved)


if __name__ == "__main__":
    raise SystemExit(main())
