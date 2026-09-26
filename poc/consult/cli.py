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
import secrets
import time
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from consult import config, jobs, logs, report, store, themes
from consult.config import Settings
from consult.configure import ConfigureError, configure, defaults
from consult.definition import Definition, DefinitionError, read_definition
from consult.fake_model import OfflineModel
from consult.ingest import IngestError, ingest
from consult.inputs import InputError
from consult.jobs import LeaseLostError
from consult.replies import ReplyError
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
    run = commands.add_parser("run-job", help="claim one find_themes job and run it to the end")
    run.add_argument("job", type=UUID, help="the job id")
    run.add_argument("--worker", required=True, help="this worker's name, for the lease")
    run.add_argument("--model", choices=["fake"], default="fake", help="the model: only the fake")
    show = commands.add_parser("themes", help="list a question's candidate themes")
    show.add_argument("question", type=UUID, help="the question id")
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
    # Find or create in one statement on the unique name, so two runs
    # racing on a new department can't each make one. The no-op SET is what
    # gets RETURNING to yield the existing row.
    found = conn.execute(
        """
        INSERT INTO department (name) VALUES (%s)
        ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """,
        (name,),
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


def _run_job(args: argparse.Namespace, settings: Settings) -> int:
    """One find_themes job, pending to succeeded, committing after every
    batch so a crash between two leaves checkpoints a takeover can resume
    from (ADR-002). The queue step stands in for dispatch (PR-08)."""
    started = time.monotonic()
    llm = OfflineModel()
    with store.connect(settings) as conn:
        jobs.queue(conn, args.job, model_alias=args.model, seed=secrets.randbelow(2**31))
        conn.commit()
        lease = jobs.claim(conn, args.job, args.worker)
        if lease is None:
            state = conn.execute("SELECT status FROM job WHERE id = %s", (args.job,)).fetchone()
            print(f"job {args.job}: not claimable ({state['status'] if state else 'unknown'})")
            return 1
        conn.commit()
        try:
            themes.run_find_themes(conn, llm, lease, after_batch=lambda _batch_no: conn.commit())
        except ReplyError as exc:
            # The failure is a code and a request id, never the reply
            # (THREAT_MODEL.md, section 2); whether it retries is the
            # reconciler's call (PR-08).
            conn.rollback()
            jobs.record_failure(conn, lease, exc.code, provider_request_id=None)
            conn.commit()
            print(f"job {args.job}: failed ({exc.code.value}: {exc.reason.value})")
            return 1
        except LeaseLostError as exc:
            conn.rollback()
            print(f"job {args.job}: {exc.code.value}")
            return 1
        conn.commit()
        summary = conn.execute(
            """
            SELECT j.question_id, q.status AS question, c.status AS consultation,
                   (SELECT string_agg(stage || ' ' || n, ', ' ORDER BY first)
                      FROM (SELECT stage, count(*) AS n, min(batch_no) AS first
                              FROM job_batch WHERE job_id = j.id GROUP BY stage) s) AS stages,
                   (SELECT count(*) FROM theme t JOIN theme_set_version v ON v.id = t.theme_set_version_id
                     WHERE v.question_id = q.id AND NOT t.is_longlist) AS shortlist,
                   (SELECT count(*) FROM theme t JOIN theme_set_version v ON v.id = t.theme_set_version_id
                     WHERE v.question_id = q.id AND t.is_longlist) AS longlist
              FROM job j JOIN question q ON q.id = j.question_id
              JOIN consultation c ON c.id = j.consultation_id
             WHERE j.id = %s
            """,
            (args.job,),
        ).fetchone()
    if summary is None:
        raise LookupError(f"job {args.job} vanished")
    counts: dict[str, int] = {}
    for part in (summary["stages"] or "").split(", "):
        stage_name, _, count = part.rpartition(" ")
        if stage_name:
            counts[stage_name] = int(count)
    logs.log_event(
        logger,
        "find_themes_run",
        job_id=args.job,
        question_id=summary["question_id"],
        status=summary["question"],
        generate_count=counts.get("generate", 0),
        condense_count=counts.get("condense", 0),
        preview_count=counts.get("preview", 0),
        shortlist_count=summary["shortlist"],
        longlist_count=summary["longlist"],
        model_call_count=len(llm.prompts),
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    batches = ", ".join(f"{n} {stage}" for stage, n in counts.items())
    print(
        f"job {args.job}: question {summary['question_id']} {summary['question']}; "
        f"{batches} batches; shortlist {summary['shortlist']}, "
        f"longlist {summary['longlist']}; consultation {summary['consultation']}"
    )
    return 0


def _themes(args: argparse.Namespace, settings: Settings) -> int:
    """The latest version's shortlist with counts and example answer ids,
    then the longlist with what each folded into. Keys, labels and numbers;
    the quotes themselves are the sign-off screen's to show (docs/02, step 8)."""
    with store.connect(settings) as conn:
        version = conn.execute(
            """
            SELECT id, version_no, status, edit_version FROM theme_set_version
             WHERE question_id = %s ORDER BY version_no DESC LIMIT 1
            """,
            (args.question,),
        ).fetchone()
        if version is None:
            print(f"question {args.question}: no theme set yet")
            return 1
        rows = conn.execute(
            """
            SELECT t.key, t.label, t.is_longlist, t.preview_count, l.key AS folded_into,
                   (SELECT string_agg(e.answer_id::text, ', ' ORDER BY e.rank)
                      FROM theme_example e WHERE e.theme_id = t.id) AS examples
              FROM theme t LEFT JOIN theme l ON l.id = t.lineage_theme_id
             WHERE t.theme_set_version_id = %s
             ORDER BY t.is_longlist, t.preview_count DESC NULLS LAST, t.key
            """,
            (version["id"],),
        ).fetchall()
    print(
        f"question {args.question}: version {version['version_no']} "
        f"({version['status']}, edit {version['edit_version']})"
    )
    print("shortlist:")
    for row in rows:
        if row["is_longlist"]:
            continue
        line = f"  {row['key']}  {report.shown(row['label'])}  count {row['preview_count']}"
        if row["examples"]:
            line += f"  examples {row['examples']}"
        print(line)
    longlist = [row for row in rows if row["is_longlist"]]
    folded = ", ".join(
        f"{row['key']} > {row['folded_into']}" if row["folded_into"] else row["key"]
        for row in longlist
    )
    print(f"longlist ({len(longlist)}): {folded}")
    return 0


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    # Here and not under __main__: the console script pyproject.toml
    # declares calls main directly, and the formatter is the control on the
    # one log path that's ours (THREAT_MODEL.md, section 2). Idempotent.
    logs.configure()
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
    if args.command == "run-job":
        return _run_job(args, resolved)
    if args.command == "themes":
        return _themes(args, resolved)
    return _validate(args, resolved)


if __name__ == "__main__":
    raise SystemExit(main())
