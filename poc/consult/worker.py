"""The model boundary the worker calls through, and what it does once a
gateway failure has been given every chance.

`consult.llm.GatewayError` is a 429 or a 5xx from the gateway (ADR-005).
`BackingOff` wraps an `LLM` so `mapping.assign` and `themes.generate` need
no change (plan section 2, "consult/worker.py"): it retries a
`GatewayError` up to `BACKOFF_ATTEMPTS` times with full-jitter backoff
(docs/02, section 9; ADR-005's "one to sixty seconds, six attempts"),
running its `before_call` immediately ahead of every attempt so no
transaction is open while the gateway is slow or the backoff sleeps.
`now()` is transaction start (PostgreSQL 17 manual, 9.9.5; PR-05's
security review, docs/07 row 05), so a heartbeat stamped after a sleep
inside an open transaction is already stale by the sleep's length, and a
transaction held open across a call keeps the job row locked for the
call's duration, which blocks the reconciler's `fail_job`.

`record_gateway_failure` is what a caller runs once the retries are
spent: the code and the request id go on the job row under the fence,
never the provider's message (CLAUDE.md, rule 8), and the log line that
reports it carries the same two fields and nothing else
(THREAT_MODEL.md, section 2).

`run_once` is the loop's one turn (docs/02, step 5; plan section 2,
"consult/worker.py"): `pick` finds the oldest runnable job, `jobs.claim`
takes it, and the job runs by kind through `themes.run_find_themes` or
`mapping.run_map_themes`, as the pipeline role, committing after every
batch. A failure goes on the row as a code under the fence, in
`cli._run_job`'s four patterns, and every job leaves one log line of
ids, counts, a status, a duration and a code.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol, TypedDict, Unpack
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from consult import jobs, logs, mapping, themes, transitions
from consult.errors import ErrorCode
from consult.jobs import MAX_ATTEMPTS, STALE_AFTER, Lease, LeaseLostError
from consult.llm import LLM, Completion, GatewayError, Prompt
from consult.replies import ReplyError
from consult.store import PIPELINE_ROLE, as_role, bound_idle_transactions

logger = logging.getLogger(__name__)

# docs/02, section 9: "Full-jitter backoff, 1 to 60 s, six attempts."
BACKOFF_BASE_SECONDS = 1
BACKOFF_CAP_SECONDS = 60
BACKOFF_ATTEMPTS = 6

# docs/02, section 9, and ADR-005 back off only on a 429 or a 5xx.
# GATEWAY_REJECTED is any other 4xx, the request's own fault (errors.py),
# so it gets one call and no wait.
RETRYABLE = frozenset(
    {ErrorCode.GATEWAY_RATE_LIMITED, ErrorCode.GATEWAY_UNAVAILABLE, ErrorCode.GATEWAY_TIMEOUT}
)


def backoff_seconds(attempt: int, rng: random.Random) -> float:
    """AWS's full jitter for the n-th retry: a draw from
    `[0, min(cap, base * 2**attempt)]` ("Exponential Backoff And Jitter",
    the formula docs/02 section 9 names as "full-jitter"), floored at one
    second, which is the design's own floor and not the formula's."""
    upper = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2**attempt)
    return max(1.0, rng.uniform(0, upper))


def call_with_backoff(
    llm: LLM,
    prompt: Prompt,
    *,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> Completion:
    """One call, retried on a `GatewayError` whose code is in `RETRYABLE`,
    up to `BACKOFF_ATTEMPTS` times with `backoff_seconds` between attempts
    and no sleep after the last. A `GatewayError` outside `RETRYABLE`
    propagates from the attempt that raised it, the same exception and not
    a new one, so it needs no `from` chain. The final retryable attempt is
    unguarded: its error, if any, is left to propagate on its own, so
    nothing here re-raises it onto a chain that could carry its message."""
    if rng is None:
        # Spread across retries, not secrecy (themes.py's shuffle reads the
        # same way; docs/02, step 6).
        rng = random.Random()  # noqa: S311  # nosec B311
    for attempt in range(BACKOFF_ATTEMPTS - 1):
        try:
            return llm.complete(prompt)
        except GatewayError as exc:
            if exc.code not in RETRYABLE:
                raise
            sleep(backoff_seconds(attempt, rng))
    return llm.complete(prompt)


@dataclass
class _BeforeEachAttempt:
    """Runs `before_call` immediately ahead of one `complete`, so
    `call_with_backoff`'s loop gets it ahead of every attempt it makes and
    not just the first."""

    llm: LLM
    before_call: Callable[[], None] | None

    def complete(self, prompt: Prompt) -> Completion:
        if self.before_call is not None:
            self.before_call()
        return self.llm.complete(prompt)


@dataclass
class BackingOff:
    """The model boundary a worker hands to `mapping.assign` and
    `themes.generate`: it satisfies `LLM`, so neither stage changes, and
    it backs off on a `GatewayError` outside whatever transaction the
    caller had open. `before_call` is where the worker passes
    `conn.commit`, which is why no call and no sleep ever finds one
    (module docstring)."""

    llm: LLM
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random | None = None
    before_call: Callable[[], None] | None = None

    def complete(self, prompt: Prompt) -> Completion:
        wrapped = _BeforeEachAttempt(self.llm, self.before_call)
        return call_with_backoff(wrapped, prompt, sleep=self.sleep, rng=self.rng)


class RunFields(TypedDict, total=False):
    """What `run_once` adds to a job's one log line besides its id, its
    attempt, its status and its code: the kind, the job's checkpoints so
    far and the run's length, each a name logs.py's allow-list passes."""

    kind: str
    batch_count: int
    duration_ms: int


def record_gateway_failure(
    conn: psycopg.Connection[DictRow],
    lease: Lease,
    exc: GatewayError,
    **fields: Unpack[RunFields],
) -> None:
    """What a caller runs once `call_with_backoff` gives up.

    Nothing needs discarding: the writes still pending at this point are
    a fenced heartbeat or a previous batch's rows, both of which stand on
    their own (the decision in plans/PR-08-poc-mapping-worker.md section
    2), so the rollback is for symmetry with the reply-error path, not
    because anything here is half-written. Then the code and the request
    id go on the job row under the fence, a commit, and a log line that
    carries the same two fields and never the provider's text. `fields`
    are `run_once`'s for that line, so a job's failure is one event.

    A stale fence surfaces as `LeaseLostError`, left to propagate with its
    own code: `run_once` turns it into a code the way `cli._run_job`'s
    four patterns do.
    """
    conn.rollback()
    try:
        jobs.record_failure(conn, lease, exc.code, provider_request_id=exc.request_id)
    except LeaseLostError:
        conn.rollback()
        raise
    conn.commit()
    logs.log_event(
        logger,
        "job_failed",
        level=logging.WARNING,
        job_id=lease.job_id,
        attempts=lease.fence,
        status="failed_retryable",
        error_code=exc.code,
        provider_request_id=exc.request_id,
        **fields,
    )


@dataclass(frozen=True)
class Outcome:
    """What one `run_once` did, for the command to print: the job, its
    kind, its status as the run left it, the attempt the run was, and the
    code when it failed. Nothing in it came from a reply."""

    job_id: UUID
    kind: str
    status: str
    attempts: int
    error_code: ErrorCode | None = None


class _Runner(Protocol):
    def __call__(
        self,
        conn: psycopg.Connection[DictRow],
        llm: LLM,
        lease: Lease,
        *,
        after_batch: Callable[[int], None] | None = None,
        before_finish: Callable[[], None] | None = None,
    ) -> transitions.Advance: ...


# The kinds a worker runs, each through the runner its module already has
# (docs/02, steps 6 and 9). The pick takes no other kind.
_RUNNERS: dict[str, _Runner] = {
    "find_themes": themes.run_find_themes,
    "map_themes": mapping.run_map_themes,
}


def pick(conn: psycopg.Connection[DictRow], *, stale_after: timedelta = STALE_AFTER) -> UUID | None:
    """The oldest runnable job, locked for the caller's claim: queued, or
    running with a lease silent past `stale_after`, and under the retry
    budget (docs/02, step 5; plan section 2, "Pick"). A queued-only pick
    would never see the stale lease a takeover needs.

    The id breaks a created_at tie, since one ingest or one sign-off gives
    every job it inserts the same `now()`. SKIP LOCKED passes over a row
    another worker's pick holds, so two workers neither wait on each other
    nor come back with the same job; the claim that follows in the same
    transaction is still the conditional UPDATE that decides.
    """
    row = conn.execute(
        """
        SELECT id FROM job
         WHERE kind = ANY(%(kinds)s) AND attempts < %(max_attempts)s
           AND (status = 'queued'
                OR (status = 'running' AND heartbeat_at < now() - %(stale_after)s))
         ORDER BY created_at, id
         LIMIT 1
           FOR UPDATE SKIP LOCKED
        """,
        {"kinds": list(_RUNNERS), "max_attempts": MAX_ATTEMPTS, "stale_after": stale_after},
    ).fetchone()
    if row is None:
        return None
    job_id: UUID = row["id"]
    return job_id


def run_once(
    conn: psycopg.Connection[DictRow],
    llm: LLM,
    *,
    worker: str,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    stale_after: timedelta = STALE_AFTER,
) -> Outcome | None:
    """Pick, claim and run one job; None when nothing is runnable.

    All of it as the pipeline role, whose grants are the control on the
    worker's path (docs/06, section 2.4), as `cli._run_job` does it. The
    pick's row lock and the claim share one transaction, committed before
    the run so the lease is visible to every other worker and to the
    reconciler. The model goes through `BackingOff` with `conn.commit`
    ahead of every attempt, and each batch commits in `after_batch`, so
    no call and no backoff sleep has a transaction open (module
    docstring). The connection comes back with none open either.

    The connection's idle transactions are bounded under the lease first
    (`store.bound_idle_transactions`), so a worker paused inside one loses
    its job to a takeover (docs/02, step 5), and the bound commits with
    the claim. The same bound can end this worker's own session while
    it's the one paused (`_disconnected`'s own docstring); `conn.closed`
    is checked before this function's own last commit too, so that
    failure doesn't ride out as a second, unrelated exception.
    """
    started = time.monotonic()
    bound_idle_transactions(conn)
    with as_role(conn, PIPELINE_ROLE):
        claimed = _claim(conn, worker, stale_after)
        conn.commit()
        outcome = (
            None
            if claimed is None
            else _run(conn, llm, *claimed, sleep=sleep, rng=rng, started=started)
        )
    # RESET ROLE opens a transaction of its own, unless conn.closed already
    # skipped it (as_role's own docstring).
    if not conn.closed:
        conn.commit()
    return outcome


def _claim(
    conn: psycopg.Connection[DictRow], worker: str, stale_after: timedelta
) -> tuple[Lease, str] | None:
    """The picked job claimed, and its kind. The pick's row lock holds the
    row as the pick found it, so the claim's predicate matches it; None
    past the pick would mean the two had drifted apart."""
    job_id = pick(conn, stale_after=stale_after)
    if job_id is None:
        return None
    lease = jobs.claim(conn, job_id, worker, stale_after=stale_after)
    row = conn.execute("SELECT kind FROM job WHERE id = %s", (job_id,)).fetchone()
    if lease is None or row is None:
        return None
    return lease, str(row["kind"])


def _run(
    conn: psycopg.Connection[DictRow],
    llm: LLM,
    lease: Lease,
    kind: str,
    *,
    sleep: Callable[[float], None],
    rng: random.Random | None,
    started: float,
) -> Outcome:
    """The claimed job run by kind, its failure recorded as a code: a
    reply the check refused as its code, a gateway error the backoff
    couldn't outlast as its code and request id, a lost lease as a code in
    the log and nothing written, and anything else as `worker_error` with
    its class in the log and its message nowhere (logs.py)."""
    model = BackingOff(llm, sleep=sleep, rng=rng, before_call=conn.commit)
    try:
        # before_finish commits too, so the finishing transaction takes the
        # consultation before the job, as fail_job does (reconciler.py).
        _RUNNERS[kind](
            conn,
            model,
            lease,
            after_batch=lambda _batch_no: conn.commit(),
            before_finish=conn.commit,
        )
    except GatewayError as exc:
        if conn.closed:
            return _disconnected(lease, kind, started)
        conn.rollback()
        fields = _fields(conn, lease, kind, started)
        try:
            record_gateway_failure(conn, lease, exc, **fields)
        except LeaseLostError as lost:
            return _lost(conn, lease, kind, lost, fields)
        return Outcome(lease.job_id, kind, "failed_retryable", lease.fence, exc.code)
    except ReplyError as exc:
        return _record(conn, lease, kind, exc.code, started)
    except LeaseLostError as exc:
        if conn.closed:
            return _disconnected(lease, kind, started)
        conn.rollback()
        return _lost(conn, lease, kind, exc, _fields(conn, lease, kind, started))
    except Exception as exc:
        return _record(conn, lease, kind, ErrorCode.WORKER_ERROR, started, error=exc)
    conn.commit()
    logs.log_event(
        logger,
        "job_run",
        job_id=lease.job_id,
        attempts=lease.fence,
        status="succeeded",
        **_fields(conn, lease, kind, started),
    )
    return Outcome(lease.job_id, kind, "succeeded", lease.fence)


def _fields(
    conn: psycopg.Connection[DictRow], lease: Lease, kind: str, started: float
) -> RunFields:
    """The job's `RunFields`. Read after a rollback, the checkpoint count
    is what committed."""
    counted = conn.execute(
        "SELECT count(*) AS batches FROM job_batch WHERE job_id = %s", (lease.job_id,)
    ).fetchone()
    return {
        "kind": kind,
        "batch_count": counted["batches"] if counted else 0,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


def _record(
    conn: psycopg.Connection[DictRow],
    lease: Lease,
    kind: str,
    code: ErrorCode,
    started: float,
    *,
    error: Exception | None = None,
) -> Outcome:
    """`cli._run_job`'s pattern for a failure the job can retry: the batch
    in flight rolled back, the code recorded under the fence and committed,
    then the log line, carrying `error`'s class when there is one. A fence
    gone stale while recording comes out as its code. A closed connection
    is left alone: `_disconnected` reports it instead, since the rollback
    below and the read `_fields` makes would each raise on it in turn."""
    if conn.closed:
        return _disconnected(lease, kind, started)
    conn.rollback()
    fields = _fields(conn, lease, kind, started)
    try:
        jobs.record_failure(conn, lease, code)
    except LeaseLostError as lost:
        conn.rollback()
        return _lost(conn, lease, kind, lost, fields)
    conn.commit()
    logs.log_event(
        logger,
        "job_failed",
        level=logging.WARNING,
        exc_info=error,
        job_id=lease.job_id,
        attempts=lease.fence,
        status="failed_retryable",
        error_code=code,
        **fields,
    )
    return Outcome(lease.job_id, kind, "failed_retryable", lease.fence, code)


def _disconnected(lease: Lease, kind: str, started: float) -> Outcome:
    """`idle_in_transaction_session_timeout` (`store.bound_idle_transactions`)
    ends the whole session, not only the transaction (PostgreSQL 17
    manual, 19.11.1), so a worker paused past the bound wakes to a
    connection the server has already closed: the write that found this
    out raised, and a rollback or a read on the same connection would
    raise the same way, so nothing here touches it again. Nothing of this
    run is written either, the server having rolled the open transaction
    back with the session, so the job's status is exactly what `claim`
    last set it to, which is `lease.fence`'s own attempt: `LEASE_LOST` is
    the nearest code in the fixed vocabulary (errors.py) to what actually
    happened, since another worker hasn't necessarily taken the lease,
    only ended this one's hold on it."""
    logs.log_event(
        logger,
        "job_failed",
        level=logging.WARNING,
        job_id=lease.job_id,
        attempts=lease.fence,
        status="running",
        error_code=ErrorCode.LEASE_LOST,
        kind=kind,
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return Outcome(lease.job_id, kind, "running", lease.fence, ErrorCode.LEASE_LOST)


def _lost(
    conn: psycopg.Connection[DictRow],
    lease: Lease,
    kind: str,
    lost: LeaseLostError,
    fields: RunFields,
) -> Outcome:
    """A lease another worker or the reconciler holds now: nothing of this
    run's is written (ADR-002), and the job's status is whatever its new
    holder has made it."""
    row = conn.execute("SELECT status FROM job WHERE id = %s", (lease.job_id,)).fetchone()
    status = str(row["status"]) if row else "unknown"
    logs.log_event(
        logger,
        "job_failed",
        level=logging.WARNING,
        job_id=lease.job_id,
        attempts=lease.fence,
        status=status,
        error_code=lost.code,
        **fields,
    )
    return Outcome(lease.job_id, kind, status, lease.fence, lost.code)
