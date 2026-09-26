"""The command line.

`consult init` applies the schema (`--reset` drops it first). `consult
validate` runs the validator over a responses file and its definition
workbook and prints the report: exit 0 with no errors, 1 when an error
blocks, 2 when the file was refused before it was read. `consult ingest`
runs validate, stage, configure and ingest in turn with every warning's
default resolution and commits once, so the proof-of-concept goes from a
spreadsheet to a schema full of rows with no model yet (docs/02, steps 2
to 3a). Its output is counts and ids, never a value from the file.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from consult import config, logs, report, store
from consult.config import Settings
from consult.configure import ConfigureError, configure, defaults
from consult.definition import Definition, DefinitionError, read_definition
from consult.ingest import IngestError, ingest
from consult.inputs import InputError
from consult.responses import Responses
from consult.stage import stage
from consult.validate import Report, validate

logger = logging.getLogger(__name__)


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
    load = commands.add_parser(
        "ingest", help="validate, stage, configure and ingest a file as a new consultation"
    )
    load.add_argument("responses", type=Path, help="the responses file, CSV or XLSX")
    load.add_argument("--definition", type=Path, required=True, help="the definition workbook")
    load.add_argument("--name", required=True, help="the consultation's name")
    load.add_argument("--department", required=True, help="the department's name, created if new")
    return parser


def _checked(args: argparse.Namespace, settings: Settings) -> tuple[Definition, Report] | int:
    """Read and validate, or the exit code to leave with. Shared by validate
    and ingest, so the two can't disagree about what blocks."""
    try:
        definition = read_definition(args.definition, settings.caps)
        responses = Responses(args.responses, settings.caps)
        result = validate(definition, responses, settings.rates)
    except InputError as exc:
        # A reason and a count, never the content (THREAT_MODEL.md, row 1).
        print(f"refused: {exc}")
        return 2
    except DefinitionError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"errors": list(exc.problems)}, indent=2))
        else:
            print(f"errors: {len(exc.problems)}")
            print("\n".join(f"  {report.shown(problem)}" for problem in exc.problems))
        return 1
    return definition, result


def _validate(args: argparse.Namespace, settings: Settings) -> int:
    checked = _checked(args, settings)
    if isinstance(checked, int):
        return checked
    definition, result = checked
    print(
        report.as_json(result)
        if args.json
        else report.render(result, args.responses, definition, args.definition)
    )
    return 1 if result.errors else 0


def _department(conn: psycopg.Connection[DictRow], name: str) -> UUID:
    found = conn.execute("SELECT id FROM department WHERE name = %s", (name,)).fetchone()
    if found is None:
        found = conn.execute(
            "INSERT INTO department (name) VALUES (%s) RETURNING id", (name,)
        ).fetchone()
    if found is None:
        raise LookupError("department insert returned no row")
    return UUID(str(found["id"]))


def _consultation(conn: psycopg.Connection[DictRow], department_id: UUID, name: str) -> UUID:
    # created_by is a user id in the design (docs/04); the proof-of-concept
    # has no users, so the row gets a fresh uuid to satisfy NOT NULL.
    created = conn.execute(
        """
        INSERT INTO consultation (department_id, name, source, created_by)
        VALUES (%s, %s, 'generic', gen_random_uuid())
        RETURNING id
        """,
        (department_id, name),
    ).fetchone()
    if created is None:
        raise LookupError("consultation insert returned no row")
    return UUID(str(created["id"]))


def _ingest(args: argparse.Namespace, settings: Settings) -> int:
    checked = _checked(args, settings)
    if isinstance(checked, int):
        return checked
    definition, result = checked
    if result.errors:
        print(report.render(result, args.responses, definition, args.definition))
        return 1
    started = time.monotonic()
    with store.connect(settings) as conn:
        department_id = _department(conn, args.department)
        consultation_id = _consultation(conn, department_id, args.name)
        staged = stage(conn, consultation_id, args.responses, settings.caps)
        try:
            configure(conn, consultation_id, definition, result, defaults(result))
            ingested = ingest(conn, consultation_id)
        except (ConfigureError, IngestError) as exc:
            # Leaving the block would commit, so roll back first: nothing of
            # a refused file stays behind, not even the consultation row.
            conn.rollback()
            print(f"refused: {exc}")
            return 1
        conn.commit()
    logs.log_event(
        logger,
        "ingested",
        consultation_id=consultation_id,
        department_id=department_id,
        row_count=staged.rows,
        respondent_count=ingested.respondents,
        answer_count=ingested.answers,
        identity_count=ingested.vault_rows,
        duplicate_answer_count=ingested.duplicate_answers,
        duplicate_respondent_count=ingested.duplicate_respondents,
        job_count=ingested.jobs,
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    print(
        f"consultation {consultation_id}: {staged.rows} rows staged, "
        f"{ingested.respondents} respondents, {ingested.answers} answers, "
        f"{ingested.vault_rows} identity rows, {ingested.duplicate_answers} duplicate answers, "
        f"{ingested.duplicate_respondents} duplicate respondents, {ingested.jobs} jobs; processing"
    )
    return 0


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
    if args.command == "ingest":
        return _ingest(args, resolved)
    return _validate(args, resolved)


if __name__ == "__main__":
    logs.configure()
    raise SystemExit(main())
