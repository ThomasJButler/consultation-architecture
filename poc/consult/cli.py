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
claims and runs one queued or stale job by kind, once with `--once` or in
a loop that stops on SIGINT or SIGTERM; `consult reconcile` runs the five
statements of docs/02 section 5 and prints their six counts.
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
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from consult import config, jobs, logs, reconciler, report, store, themes, transitions, worker
from consult.config import Settings
from consult.configure import ConfigureError, configure, defaults
from consult.definition import Definition, DefinitionError, read_definition
from consult.dispatch import dispatch
from consult.fake_model import OfflineModel
from consult.ingest import IngestError, ingest
from consult.inputs import InputError
from consult.jobs import LeaseLostError
from consult.llm import GatewayError
from consult.replies import ReplyError
from consult.responses import Responses
from consult.stage import INGEST_ROLE, stage
from consult.store import PIPELINE_ROLE, as_role
from consult.validate import Report, validate
from consult.worker import Outcome

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
        f"{ingested.respondents} respondents, {ingested.answers} answers, "
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
        # Dispatched, then claimed and run as the pipeline role, whose
        # grants are the control on the worker's path (docs/06, section
        # 2.4): SET ROLE outlives the commits between batches, and RESET
        # ROLE follows the last one.
        with as_role(conn, PIPELINE_ROLE):
            dispatch(conn, settings)
            conn.commit()
            lease = jobs.claim(conn, args.job, args.worker)
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
                themes.run_find_themes(
                    conn, model, lease, after_batch=lambda _batch_no: conn.commit()
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
            SELECT t.key, t.label, t.description, t.is_longlist, t.preview_count,
                   l.key AS folded_into,
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
            print(
                f"question {args.question}: conflict, the list has moved on from edit "
                f"{args.expect_version}"
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


def _print_outcome(outcome: Outcome | None) -> None:
    if outcome is None:
        print("nothing to run")
        return
    line = f"job {outcome.job_id}: {outcome.kind} {outcome.status}, attempt {outcome.attempts}"
    if outcome.error_code is not None:
        line += f", {outcome.error_code.value}"
    print(line)


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
    """A flag SIGINT and SIGTERM both set, so the loop below finishes the
    job it is on and stops rather than dying mid-batch (ADR-002: "on
    SIGTERM a worker finishes its current batch"). Installed only here,
    since `--once` returns before a second signal could matter."""
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
    loop isn't run by a test: real time isn't something a test should
    wait on, and `--once` is what `test_cli_worker.py` drives instead."""
    llm = OfflineModel()
    name = _worker_name(args.worker)
    with store.connect(settings) as conn:
        if args.once:
            outcome = worker.run_once(conn, llm, worker=name)
            _print_outcome(outcome)
            _log_outcome(outcome)
            return 0
        stop = _stop_flag()
        while not stop.is_set():
            outcome = worker.run_once(conn, llm, worker=name)
            _print_outcome(outcome)
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
    return _validate(args, resolved)


if __name__ == "__main__":
    raise SystemExit(main())
