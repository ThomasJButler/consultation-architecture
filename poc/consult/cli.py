"""The command line.

`consult init` applies the schema (`--reset` drops it first). `consult
validate` runs the validator over a responses file and its definition
workbook and prints the report: exit 0 with no errors, 1 when an error
blocks, 2 when the file was refused before it was read. `consult ingest`
runs validate, stage, configure and ingest in turn with every warning's
default resolution, dispatches the jobs it inserted and commits once, so
the proof-of-concept goes from a spreadsheet to a schema full of rows with
no model yet (docs/02, steps 2 to 4). Its output is counts and ids, never
a value from the file. `consult run-job` claims one find_themes job and
runs it to the end with the fake, committing after every batch; `consult
themes` lists a question's candidates as keys, labels, counts and answer
ids; `consult sign-off` confirms them as they stand and dispatches the
map_themes job (docs/02, steps 4 and 6 to 8). `consult worker` picks,
claims and runs one queued or stale job by kind, once with `--once` or,
without it, in a loop that stops between jobs on SIGINT or SIGTERM: a
manual convenience, not ADR-002's per-batch stop inside Fargate's
`stopTimeout`, which is the deployed worker's design (`_stop_flag`'s
docstring). `consult reconcile` runs the five statements of docs/02
section 5 and prints their six counts. `consult query QUESTION [--filter
F]...` prints docs/02 screen 4's per-question dashboard: the theme table
with its denominator, and the related closed question's distribution,
under a filter of the four forms step 11 names; a malformed one is
refused by its code and never by the value that failed it. `consult
export CONSULTATION --out PATH` writes docs/02 step 12's XLSX workbook and
prints its counts. Neither prints an answer's text.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from consult import (
    config,
    export,
    jobs,
    logs,
    query,
    reconciler,
    report,
    store,
    themes,
    transitions,
    worker,
)
from consult.config import Settings
from consult.configure import ConfigureError, configure, defaults
from consult.definition import Definition, DefinitionError, read_definition
from consult.dispatch import dispatch
from consult.errors import ErrorCode
from consult.fake_model import OfflineModel
from consult.ingest import IngestError, ingest
from consult.inputs import InputError
from consult.jobs import LeaseLostError
from consult.llm import GatewayError
from consult.query import FilterError
from consult.replies import ReplyError
from consult.responses import Responses
from consult.stage import INGEST_ROLE, stage
from consult.store import PIPELINE_ROLE, as_role
from consult.validate import Report, validate
from consult.worker import Outcome

# Named explicitly, not by __name__: the Makefile's targets run this
# module as __main__ (`python -m consult.cli`), where __name__ is
# "__main__" and every line here would log under that name instead of
# consult.cli, the name .venv/bin/consult's console script gives it.
logger = logging.getLogger("consult.cli")


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
    confirm = commands.add_parser("sign-off", help="confirm a question's themes as they stand")
    confirm.add_argument("question", type=UUID, help="the question id")
    confirm.add_argument("--reviewer", type=UUID, required=True, help="the reviewer's user id")
    confirm.add_argument(
        "--expect-version",
        type=int,
        required=True,
        help="the edit counter the reviewer was looking at (docs/02, screen 3)",
    )
    run_worker = commands.add_parser(
        "worker", help="pick, claim and run one queued or stale job by kind"
    )
    run_worker.add_argument(
        "--once", action="store_true", help="run one job and exit; without it, loop until stopped"
    )
    run_worker.add_argument(
        "--worker", help="this worker's name, for the lease (default: hostname and pid)"
    )
    run_worker.add_argument(
        "--model", choices=["fake"], default="fake", help="the model: only the fake"
    )
    run_worker.add_argument(
        "--poll-seconds",
        type=float,
        default=5.0,
        help="how long to sleep between empty picks when looping (default 5)",
    )
    commands.add_parser("reconcile", help="run the reconciler's five statements once")
    run_query = commands.add_parser(
        "query", help="print a question's theme table and related distribution under a filter"
    )
    run_query.add_argument("question", type=UUID, help="the question id")
    run_query.add_argument(
        "--filter",
        action="append",
        default=[],
        help=(
            "attr:<column>=<value>, theme:<key> (OR'd, repeatable), "
            "other:<column_ref>.theme=<key>, e.g. other:o_safety.theme=ACCESS, "
            "or with=duplicates (docs/02, step 11); repeatable"
        ),
    )
    run_query.add_argument(
        "--department",
        type=UUID,
        help="the caller's department id (docs/06, section 2); default: the question's own",
    )
    run_export = commands.add_parser("export", help="write the consultation's XLSX workbook")
    run_export.add_argument("consultation", type=UUID, help="the consultation id")
    run_export.add_argument("--out", type=Path, required=True, help="the workbook's path")
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
    # Find or create on the unique name, so two runs racing on a new
    # department can't each make one. DO NOTHING takes no lock on a row
    # that's already there, where a DO UPDATE setting the unique column
    # would lock it FOR UPDATE until the ingest commits, and that blocks
    # the FOR KEY SHARE every insert referencing department takes, a
    # worker's checkpoints and tags among them (PostgreSQL 17 manual,
    # 13.3.2). A racing insert of the same name makes DO NOTHING wait for
    # it to commit, so the SELECT that follows finds its row.
    created = conn.execute(
        "INSERT INTO department (name) VALUES (%s) ON CONFLICT (name) DO NOTHING RETURNING id",
        (name,),
    ).fetchone()
    found = created or conn.execute("SELECT id FROM department WHERE name = %s", (name,)).fetchone()
    if found is None:
        raise LookupError("department neither inserted nor found")
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
        # The request that inserts a job dispatches it in the same breath
        # (docs/02, step 4): what the caps let through commits queued.
        # Under the ingest role, which plan section 2 gives UPDATE on job
        # for this (docs/06, section 2.4).
        with as_role(conn, INGEST_ROLE):
            dispatch(conn, settings)
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
        f"{ingested.respondents} respondents, {ingested.answers} answer rows, "
        f"{ingested.vault_rows} identity rows, {ingested.duplicate_answers} duplicate answers, "
        f"{ingested.duplicate_respondents} duplicate respondents, {ingested.jobs} jobs; processing"
    )
    return 0


def _run_job(args: argparse.Namespace, settings: Settings) -> int:
    """One find_themes job run to succeeded, committing after every batch
    so a crash between two leaves checkpoints a takeover can resume from
    (ADR-002). Dispatch runs first, so a job still pending is queued if
    the caps let it through (docs/02, step 4)."""
    started = time.monotonic()
    llm = OfflineModel()
    with store.connect(settings) as conn:
        # Bounded as the worker's connection is: a run paused mid-batch
        # loses its job to a takeover (store.bound_idle_transactions).
        store.bound_idle_transactions(conn)
        # Dispatched, then claimed and run as the pipeline role, whose
        # grants are the control on the worker's path (docs/06, section
        # 2.4): SET ROLE outlives the commits between batches, and RESET
        # ROLE follows the last one.
        with as_role(conn, PIPELINE_ROLE):
            dispatch(conn, settings)
            conn.commit()
            # The command runs find_themes and nothing else (its help
            # line), and a job at the retry budget is the reconciler's to
            # fail (ADR-002; docs/02, section 5): the claim refuses both.
            lease = jobs.claim(conn, args.job, args.worker, kind="find_themes")
            if lease is None:
                state = conn.execute("SELECT status FROM job WHERE id = %s", (args.job,)).fetchone()
                print(f"job {args.job}: not claimable ({state['status'] if state else 'unknown'})")
                conn.rollback()
                return 1
            conn.commit()
            # BackingOff with conn.commit as before_call, the same as the
            # worker loop (worker.py's module docstring): no call and no
            # backoff sleep is left with a transaction open.
            model = worker.BackingOff(llm, before_call=conn.commit)
            try:
                # before_finish commits too, as worker._run's does, so the
                # finish locks the consultation before the job (reconciler.py).
                themes.run_find_themes(
                    conn,
                    model,
                    lease,
                    after_batch=lambda _batch_no: conn.commit(),
                    before_finish=conn.commit,
                )
            except GatewayError as exc:
                # record_gateway_failure does its own rollback and commit
                # (its docstring), the code and request id under the
                # fence, never the provider's text (CLAUDE.md, rule 8).
                try:
                    worker.record_gateway_failure(conn, lease, exc)
                except LeaseLostError as lost:
                    conn.rollback()
                    print(f"job {args.job}: {lost.code.value}")
                    return 1
                print(f"job {args.job}: failed ({exc.code.value})")
                return 1
            except ReplyError as exc:
                # The failure is a code and a request id, never the reply
                # (THREAT_MODEL.md, section 2); whether it retries is the
                # reconciler's call (PR-08). Recording it is a fenced write
                # too, so the lease can turn out to have gone here as well.
                conn.rollback()
                try:
                    jobs.record_failure(conn, lease, exc.code, provider_request_id=None)
                except LeaseLostError as lost:
                    conn.rollback()
                    print(f"job {args.job}: {lost.code.value}")
                    return 1
                conn.commit()
                print(f"job {args.job}: failed ({exc.code.value}: {exc.reason.value})")
                return 1
            except LeaseLostError as exc:
                conn.rollback()
                print(f"job {args.job}: {exc.code.value}")
                return 1
            except Exception as exc:
                # worker._run's fourth branch: anything else is worker_error
                # under the fence, so the claim doesn't sit running with no
                # code until the lease goes stale. The class is printed and
                # the message nowhere, since it could carry an answer
                # (CLAUDE.md, rule 8).
                conn.rollback()
                try:
                    jobs.record_failure(conn, lease, ErrorCode.WORKER_ERROR)
                except LeaseLostError as lost:
                    conn.rollback()
                    print(f"job {args.job}: {lost.code.value}")
                    return 1
                conn.commit()
                print(
                    f"job {args.job}: failed ({ErrorCode.WORKER_ERROR.value}: {type(exc).__name__})"
                )
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
        exists = conn.execute("SELECT 1 FROM question WHERE id = %s", (args.question,)).fetchone()
        if exists is None:
            print(f"question {args.question}: not found")
            return 1
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
        # lineage_theme_id carries two different meanings depending on when
        # it was set: themes.condense sets it, within one version, to the
        # shortlist theme a longlist entry folded into; transitions.sign_off
        # then reuses the same column, across versions, to point a copied
        # row at the candidate row it was copied from. `source` resolves
        # the second meaning; when a row is native to its own version (a
        # candidate, not yet signed off), source already IS the fold
        # target, so the CASE reads its key directly there rather than
        # hopping again through `folded`, which would be the shortlist
        # row's own (always null) lineage. Examples ride the same source
        # pointer: sign_off doesn't copy theme_example, so a signed-off
        # row's examples are the row it was copied from carries.
        rows = conn.execute(
            """
            SELECT t.key, t.label, t.description, t.is_longlist, t.preview_count,
                   CASE WHEN source.theme_set_version_id = t.theme_set_version_id
                        THEN source.key ELSE folded.key END AS folded_into,
                   (SELECT string_agg(e.answer_id::text, ', ' ORDER BY e.rank)
                      FROM theme_example e
                     WHERE e.theme_id = coalesce(t.lineage_theme_id, t.id)) AS examples
              FROM theme t
              LEFT JOIN theme source ON source.id = t.lineage_theme_id
              LEFT JOIN theme folded ON folded.id = source.lineage_theme_id
             WHERE t.theme_set_version_id = %s
             ORDER BY t.is_longlist, t.preview_count DESC NULLS LAST, t.key
            """,
            (version["id"],),
        ).fetchall()
    # --expect-version wants the edit counter, not the version number, so
    # a candidate's own line names the value to pass sign-off next.
    hint = (
        f"; sign off with --expect-version {version['edit_version']}"
        if version["status"] == "candidate"
        else ""
    )
    print(
        f"question {args.question}: version {version['version_no']} "
        f"({version['status']}, edit {version['edit_version']}{hint})"
    )
    print("shortlist:")
    for row in rows:
        if row["is_longlist"]:
            continue
        # A fallback theme (OTHER, NO_REASON) carries no preview_count of
        # its own: never previewed, so never counted (transitions.sign_off).
        count = row["preview_count"] if row["preview_count"] is not None else "-"
        line = f"  {row['key']}  {report.shown(row['label'])}  count {count}"
        if row["examples"]:
            line += f"  examples {row['examples']}"
        print(line)
        if row["description"]:
            # What sign-off freezes and every mapping prompt will carry; the
            # reviewer has to have read it here (the security review, docs/07).
            print(f"      {report.shown(row['description'])}")
    longlist = [row for row in rows if row["is_longlist"]]
    folded = ", ".join(
        f"{row['key']} > {row['folded_into']}" if row["folded_into"] else row["key"]
        for row in longlist
    )
    print(f"longlist ({len(longlist)}): {folded}")
    return 0


def _sign_off(args: argparse.Namespace, settings: Settings) -> int:
    """Confirm as-is (docs/02, step 8; ADR-003). The guarded UPDATE is the
    mutex and the edit counter is checked in the statement that supersedes
    the candidate, so a reviewer who saw an older list gets a conflict. The
    map_themes job it inserts is dispatched before the commit (step 4)."""
    with store.connect(settings) as conn:
        try:
            signed = transitions.sign_off(
                conn, args.question, args.reviewer, expected_version=args.expect_version
            )
        except transitions.SignOffConflictError:
            conn.rollback()
            # The candidate itself, not the caller's stale guess: read
            # after the refusal, so the message names where the list
            # actually is rather than assuming it moved past what was asked.
            current = conn.execute(
                """
                SELECT edit_version FROM theme_set_version
                 WHERE question_id = %s AND status = 'candidate'
                 ORDER BY version_no DESC LIMIT 1
                """,
                (args.question,),
            ).fetchone()
            at = current["edit_version"] if current is not None else "unknown"
            print(
                f"question {args.question}: conflict, expected edit "
                f"{args.expect_version}, the list is at edit {at}"
            )
            return 1
        except transitions.TransitionError:
            conn.rollback()
            print(f"question {args.question}: refused, no candidate version to sign off")
            return 1
        if signed is None:
            conn.rollback()
            print(f"question {args.question}: refused, not awaiting sign-off")
            return 1
        # The proof-of-concept has no web-app role; the pipeline role's
        # grants (SELECT on job and department, UPDATE on job) are what
        # dispatch needs here too (docs/06, section 2.4).
        with as_role(conn, PIPELINE_ROLE):
            dispatch(conn, settings)
        conn.commit()
        # Read back rather than assumed: the caps can leave the job pending.
        status = conn.execute(
            """
            SELECT j.status AS job, c.status AS consultation
              FROM job j JOIN consultation c ON c.id = j.consultation_id
             WHERE j.id = %s
            """,
            (signed.job_id,),
        ).fetchone()
    logs.log_event(
        logger,
        "signed_off",
        question_id=args.question,
        version_id=signed.version_id,
        job_id=signed.job_id,
        reviewer_id=args.reviewer,
    )
    print(
        f"question {args.question} signed off: version {signed.version_id}, "
        f"map_themes job {signed.job_id} {status['job'] if status else 'unknown'}; "
        f"consultation {status['consultation'] if status else 'unknown'}"
    )
    return 0


def _worker_name(name: str | None) -> str:
    """The default lease name: this host and this process, so two workers
    started on one machine with no name still get distinct ones (docs/02,
    step 5)."""
    return name or f"{socket.gethostname()}-{os.getpid()}"


def _print_outcome(conn: psycopg.Connection[DictRow], outcome: Outcome | None) -> int:
    """Prints the outcome and returns the exit code: 0 for nothing to run
    or a success, 1 when the run's own attempt failed. The question and
    consultation are read back rather than assumed, the same as
    `_run_job`'s summary, so a script reading this line sees the states
    the job actually left rather than having to reach for psql."""
    if outcome is None:
        print("nothing to run")
        return 0
    line = f"job {outcome.job_id}: {outcome.kind} {outcome.status}, attempt {outcome.attempts}"
    if outcome.error_code is not None:
        line += f", {outcome.error_code.value}"
    state = conn.execute(
        """
        SELECT j.question_id, q.status AS question, c.status AS consultation
          FROM job j JOIN question q ON q.id = j.question_id
          JOIN consultation c ON c.id = j.consultation_id
         WHERE j.id = %s
        """,
        (outcome.job_id,),
    ).fetchone()
    if state is not None:
        line += (
            f"; question {state['question_id']} {state['question']}; "
            f"consultation {state['consultation']}"
        )
    print(line)
    return 1 if outcome.error_code is not None else 0


def _log_outcome(outcome: Outcome | None) -> None:
    if outcome is None:
        logs.log_event(logger, "worker_idle")
        return
    if outcome.error_code is None:
        logs.log_event(
            logger,
            "worker_run",
            job_id=outcome.job_id,
            kind=outcome.kind,
            status=outcome.status,
            attempts=outcome.attempts,
        )
    else:
        logs.log_event(
            logger,
            "worker_run",
            job_id=outcome.job_id,
            kind=outcome.kind,
            status=outcome.status,
            attempts=outcome.attempts,
            error_code=outcome.error_code,
        )


def _stop_flag() -> threading.Event:
    """A flag SIGINT and SIGTERM both set, checked only between
    `run_once` calls (`_worker`, below), so the loop stops between jobs,
    not between batches. ADR-002's "on SIGTERM a worker finishes its
    current batch and stops" is sized to `stopTimeout` for the deployed
    Fargate worker; this loop is the manual test's own convenience, and
    `--once` is the path a test drives (`_worker`'s docstring). Installed
    only here, since `--once` returns before a second signal could
    matter."""
    stop = threading.Event()

    def _handle(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    return stop


def _worker(args: argparse.Namespace, settings: Settings) -> int:
    """`--once` runs `worker.run_once` once and prints its outcome or
    "nothing to run"; `run_once` already picks, claims and runs the job as
    the pipeline role and leaves no transaction open (its own module
    docstring), so this command sets no role itself. Without `--once` it
    loops, sleeping `--poll-seconds` between empty picks, until SIGINT or
    SIGTERM flips the stop flag; the design has no number for that sleep,
    so five seconds is picked only to be short enough not to leave real
    work waiting and long enough not to poll Postgres for nothing. The
    flag is only read between the calls to `run_once` below, so a job
    already running is finished whole; that is coarser than ADR-002's
    per-batch stop inside `stopTimeout`, which belongs to the deployed
    worker and not to this loop's own manual convenience (`_stop_flag`'s
    docstring). The loop isn't run by a test: real time isn't something a
    test should wait on, and `--once` is what `test_cli_worker.py` drives
    instead."""
    llm = OfflineModel()
    name = _worker_name(args.worker)
    with store.connect(settings) as conn:
        if args.once:
            outcome = worker.run_once(conn, llm, worker=name)
            code = _print_outcome(conn, outcome)
            _log_outcome(outcome)
            return code
        stop = _stop_flag()
        while not stop.is_set():
            outcome = worker.run_once(conn, llm, worker=name)
            _print_outcome(conn, outcome)
            _log_outcome(outcome)
            if outcome is None:
                stop.wait(args.poll_seconds)
    return 0


def _reconcile(args: argparse.Namespace, settings: Settings) -> int:
    """`reconciler.reconcile` already runs every statement as the pipeline
    role (its own module docstring), so this command sets no role either:
    dispatch touches `job` and `fail_job` writes `consultation`, both
    grants the pipeline role already carries (docs/06, section 2.4)."""
    with store.connect(settings) as conn:
        reconciled = reconciler.reconcile(conn, settings)
    print(
        f"reconciled: {reconciled.dispatched} dispatched, {reconciled.resent} resent, "
        f"{reconciled.failed} failed, {reconciled.retried} retried, "
        f"{reconciled.advanced} advanced, {reconciled.relayed} relayed"
    )
    return 0


def _question_department(conn: psycopg.Connection[DictRow], question_id: UUID) -> UUID:
    row = conn.execute(
        "SELECT department_id FROM question WHERE id = %s", (question_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"question {question_id} does not exist")
    department_id: UUID = row["department_id"]
    return department_id


def _query(args: argparse.Namespace, settings: Settings) -> int:
    """docs/02 screen 4's per-question dashboard, printed rather than paged.
    The filter parses to a typed value first, so a malformed one is
    refused by its code and exits 2 before any query runs, and the value
    that failed it never reaches the line (CLAUDE.md rule 8: a filter
    value is whatever a user typed into the address bar). The two reads
    run under `store.PIPELINE_ROLE`: the proof-of-concept has no
    dashboard role of its own, the pipeline role holds every SELECT the
    two reads need (schema.sql's grant on all public tables) and no
    grant on the vault at all, so a read this path should never make
    fails at the schema rather than succeeding (docs/06, section 2.4).

    Every query is held to the caller's department (docs/06, section 2).
    The web app passes the signed-in user's; the command-line operator
    is trusted to name one with `--department`, and without it the
    command reads the question's own first, under the same role.

    The theme table, the related distribution and the with-duplicates
    theme table run in one REPEATABLE READ, read-only transaction, the
    same reasoning as `export.write_workbook`'s own gather: three
    statements under READ COMMITTED would each take their own snapshot
    (Postgres documentation, 13.2.1), and `hidden`, below, is a
    difference between two of them, so a tag batch, a retraction or a
    sign-off committed between the calls would move it.
    """
    try:
        parsed = query.parse_filters(args.filter or [])
    except FilterError as exc:
        print(f"refused: {exc}")
        return 2
    with store.connect(settings) as conn:
        # One REPEATABLE READ, read-only snapshot for the reads below, the
        # same fix export.write_workbook uses for its own gather: a READ
        # COMMITTED transaction gives each statement its own snapshot
        # (Postgres documentation, 13.2.1), so a tag batch, a retraction or
        # a sign-off committed between the first theme_table call and the
        # with-duplicates one would otherwise move the hidden count below.
        # Set before as_role's SET ROLE, the first statement psycopg sends,
        # so it applies to this transaction and not the next; the
        # connection is this command's own, opened fresh above, so nothing
        # needs restoring once it closes.
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        with as_role(conn, PIPELINE_ROLE):
            try:
                department_id = args.department or _question_department(conn, args.question)
                query.check_filter_names(conn, args.question, parsed, department_id=department_id)
                table = query.theme_table(conn, args.question, parsed, department_id=department_id)
                distribution = query.related_distribution(
                    conn, args.question, parsed, department_id=department_id
                )
                # What with=duplicates would add: the same scope with the
                # two IS NULL predicates dropped, less what the default
                # scope already counts (docs/02 section 7, decision 9).
                # Naming it is cli.py's half of the finding export.py's
                # own summary line already carries.
                shown = query.theme_table(
                    conn,
                    args.question,
                    replace(parsed, with_duplicates=True),
                    department_id=department_id,
                )
            except LookupError:
                print(f"question {args.question}: not found")
                return 1
            except FilterError as exc:
                print(f"refused: {exc}")
                return 2
    hidden = shown.denominator - table.denominator
    logs.log_event(
        logger,
        "queried",
        question_id=args.question,
        theme_count=len(table.rows),
        respondent_count=table.denominator,
        hidden_count=hidden,
    )
    line = f"question {args.question}: of {table.denominator} respondents who answered"
    if hidden:
        line += f" ({hidden} duplicate answers hidden; add --filter with=duplicates to count them)"
    print(line)
    for row in table.rows:
        pct = (row.respondents / table.denominator * 100) if table.denominator else 0.0
        print(f"  {row.key}  {report.shown(row.label)}  {row.respondents}  {pct:.1f}%")
    if distribution:
        print(
            "related: "
            + ", ".join(f"{report.shown(label)} {count}" for label, count in distribution)
        )
    return 0


def _export(args: argparse.Namespace, settings: Settings) -> int:
    """docs/02 step 12's XLSX. `write_workbook` already runs its own reads
    under `store.EXPORT_ROLE` (export.py's own docstring), so this command
    sets no role itself, only the path and the timing.
    """
    started = time.monotonic()
    try:
        with store.connect(settings) as conn:
            result = export.write_workbook(conn, args.consultation, args.out)
    except LookupError:
        print(f"consultation {args.consultation}: not found")
        return 1
    except export.ExportError as exc:
        # The code only: THREAT_MODEL.md section 2 forbids the value that
        # failed a cell riding an exception message, and ExportError's own
        # docstring carries nothing else.
        print(f"refused: {exc.code}")
        return 1
    logs.log_event(
        logger,
        "exported",
        consultation_id=args.consultation,
        respondent_count=result.respondents,
        answer_count=result.answers,
        tag_count=result.tags,
        sheet_count=result.sheets,
        truncated_cell_count=result.truncated_cells,
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    line = (
        f"wrote {args.out}: {result.respondents} respondents, {result.answers} open answers, "
        f"{result.tags} tags, {result.sheets} sheets"
    )
    if result.truncated_cells:
        line += f", {result.truncated_cells} cells truncated at the cap"
    print(line)
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
    if args.command == "sign-off":
        return _sign_off(args, resolved)
    if args.command == "worker":
        return _worker(args, resolved)
    if args.command == "reconcile":
        return _reconcile(args, resolved)
    if args.command == "query":
        return _query(args, resolved)
    if args.command == "export":
        return _export(args, resolved)
    return _validate(args, resolved)


if __name__ == "__main__":
    raise SystemExit(main())
