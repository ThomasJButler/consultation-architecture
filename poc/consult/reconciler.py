"""The reconciler's statements (docs/02, section 5).

Every five minutes, in order, each idempotent, and each its own
transaction or run of transactions: a second pass over what the first
left changes nothing. The job table is the truth and a message is a hint
(ADR-006, last paragraph), so the proof-of-concept's "re-send" is a fresh
`sent_at` and nothing more: nothing here claims, and a worker's
conditional claim is still what decides who runs a job (docs/02, step 5).

Lock order is `transitions.fail_job`'s: the consultation row, then the
job row, which is the order a worker's finishing transaction takes them
in too. So a statement never holds a job row lock when it asks for a
consultation's: its bulk UPDATE commits first, and each job at the retry
budget is then failed in a transaction of its own that locks the
consultation before the job (`_fail_each`). The worker keeps the
same order by committing before its finish (the runners' `before_finish`,
which `worker._run` and `cli._run_job` pass as `conn.commit`): a takeover
with nothing left to send has only a heartbeat to its name, and that
heartbeat's job row lock would otherwise still be held when the finish
asks for the consultation.

Three things the design has that this module doesn't. A row the relay
left in `sending` by crashing between its two commits needs ADR-006's
reference lookup, asking Notify for a notification with that reference
before sending again; with no Notify here there's nothing to ask, so
such a row stays where it is. Statement 5 as corrected (docs/02,
correction 7) also inserts `review_reminder` rows, keyed on five working
days in `themes_ready`, which nothing records the start of; plan
section 0 leaves them out, so the relay relays and does nothing more.
And ADR-006 has two relays sharing one query: the worker's fast path,
right after the commit that wrote the outbox row, and statement 5 as the
slow path. `worker.run_once` doesn't relay, so every email here waits
for the next pass, up to its five minutes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import LiteralString

import psycopg
from psycopg.rows import DictRow

from consult import dispatch, logs, transitions
from consult.config import Settings
from consult.jobs import STALE_AFTER
from consult.store import PIPELINE_ROLE, as_role
from consult.worker import MAX_ATTEMPTS

logger = logging.getLogger(__name__)

# ADR-006's relay query takes twenty rows at a time.
RELAY_LIMIT = 20

# Each spent scan's own test, again for the one job `_fail_each` is about
# to fail, under the consultation's lock (`recover` and `retry`).
_STILL_STALE = """
    SELECT id FROM job
     WHERE id = %(job_id)s
       AND status IN ('queued', 'running') AND attempts >= %(max_attempts)s
       AND CASE status WHEN 'queued' THEN sent_at ELSE heartbeat_at END
           < now() - %(stale_after)s
       FOR UPDATE SKIP LOCKED
"""
_STILL_DUE = """
    SELECT id FROM job
     WHERE id = %(job_id)s
       AND status = 'failed_retryable' AND next_attempt_at <= now()
       AND attempts >= %(max_attempts)s
       FOR UPDATE SKIP LOCKED
"""


@dataclass(frozen=True)
class Reconciled:
    """What one pass did, statement by statement, for the command to print
    and log. Counts only, so nothing from a reply or an answer can ride
    along."""

    dispatched: int
    resent: int
    failed: int  # jobs fail_job marked failed, statements 2 and 3 together
    retried: int
    advanced: int  # consultations either fan-in moved
    relayed: int


def reconcile(conn: psycopg.Connection[DictRow], settings: Settings) -> Reconciled:
    """The five statements in order, each committed before the next
    starts, so a failure in one leaves the ones before it done (docs/02,
    section 5). Dispatch is step 4's own statement, the slow path for a
    crash or a slot the caps have just freed.

    All of it as the pipeline role, whose grants are the control on the
    pipeline's path (docs/06, section 2.4), as `worker.run_once` runs, so
    the command needn't set one. One log line carries the six counts.
    """
    with as_role(conn, PIPELINE_ROLE):
        dispatched = dispatch.dispatch(conn, settings)
        conn.commit()
        resent, failed_stale = recover(conn)
        conn.commit()
        retried, failed_due = retry(conn)
        conn.commit()
        advanced = rerun_fan_ins(conn)
        conn.commit()
        relayed = relay(conn)
        conn.commit()
    # RESET ROLE opens a transaction of its own.
    conn.commit()
    reconciled = Reconciled(
        dispatched=dispatched,
        resent=resent,
        failed=failed_stale + failed_due,
        retried=retried,
        advanced=advanced,
        relayed=relayed,
    )
    logs.log_event(
        logger,
        "reconciled",
        dispatched_count=reconciled.dispatched,
        resent_count=reconciled.resent,
        failed_count=reconciled.failed,
        retried_count=reconciled.retried,
        advanced_count=reconciled.advanced,
        relayed_count=reconciled.relayed,
    )
    return reconciled


def recover(
    conn: psycopg.Connection[DictRow],
    *,
    stale_after: timedelta = STALE_AFTER,
    max_attempts: int = MAX_ATTEMPTS,
) -> tuple[int, int]:
    """Statement 2: re-send a stale job below the retry budget and fail one
    at it; returns (re-sent, failed).

    Stale is docs/02's own test: queued with a send older than the lease,
    or running with a heartbeat as old. The re-send also needs the send
    itself to be that old, which is news only for a running job: its
    heartbeat stays stale until a worker takes it over, so without that
    every pass would re-send it, and a re-send opens a ten-minute window
    of its own, as dispatch's send does for a queued job (ADR-006: "a lost
    message is caught by the ten-minute re-send").

    Only a kind `fail_job` has a failed edge for is failed: any other
    would raise on every pass and stop the statements after this one, so
    it's left where it is.

    The re-send is one UPDATE, committed before any job is failed, so no
    job row lock is held when `fail_job` takes the consultation's (module
    docstring). It takes its rows FOR UPDATE SKIP LOCKED: a job row some
    other transaction holds is a worker mid-batch whose heartbeat hasn't
    committed yet, so the lease is live and the row is left for a later
    pass, as `worker.pick` leaves it. Waiting on it would hold up every
    statement after this one for as long as that worker stays paused.
    """
    params = {
        "stale_after": stale_after,
        "max_attempts": max_attempts,
        "kinds": list(transitions.FAILABLE_KINDS),
    }
    resent = conn.execute(
        """
        UPDATE job SET sent_at = now()
         WHERE id IN (
               SELECT id FROM job
                WHERE status IN ('queued', 'running') AND attempts < %(max_attempts)s
                  AND CASE status WHEN 'queued' THEN sent_at ELSE heartbeat_at END
                      < now() - %(stale_after)s
                  AND sent_at < now() - %(stale_after)s
                  FOR UPDATE SKIP LOCKED)
        """,
        params,
    ).rowcount
    conn.commit()
    spent = conn.execute(
        """
        SELECT id, consultation_id FROM job
         WHERE status IN ('queued', 'running') AND attempts >= %(max_attempts)s
           AND kind = ANY(%(kinds)s)
           AND CASE status WHEN 'queued' THEN sent_at ELSE heartbeat_at END
               < now() - %(stale_after)s
         ORDER BY id
        """,
        params,
    ).fetchall()
    return resent, _fail_each(conn, spent, _STILL_STALE, params)


def retry(
    conn: psycopg.Connection[DictRow], *, max_attempts: int = MAX_ATTEMPTS
) -> tuple[int, int]:
    """Statement 3: a due failure below the retry budget goes back to
    pending, and one at it is failed; returns (retried, failed).

    The section retried only below five and statement 2 scans only queued
    and running, so a job that failed on its fifth attempt sat in
    failed_retryable for good (plan section 2). Failing it here closes
    that, for the kinds `fail_job` can fail, as in `recover`.

    Only the status moves on a retry. attempts, params and model_alias
    stay, so the next dispatch keeps the seed and the alias (ADR-002), and
    error_code stays until the next failure overwrites it. As in
    `recover`, the UPDATE commits before any job is failed.
    """
    params = {"max_attempts": max_attempts, "kinds": list(transitions.FAILABLE_KINDS)}
    retried = conn.execute(
        """
        UPDATE job SET status = 'pending'
         WHERE status = 'failed_retryable' AND next_attempt_at <= now()
           AND attempts < %(max_attempts)s
        """,
        params,
    ).rowcount
    conn.commit()
    spent = conn.execute(
        """
        SELECT id, consultation_id FROM job
         WHERE status = 'failed_retryable' AND next_attempt_at <= now()
           AND attempts >= %(max_attempts)s AND kind = ANY(%(kinds)s)
         ORDER BY id
        """,
        params,
    ).fetchall()
    return retried, _fail_each(conn, spent, _STILL_DUE, params)


def rerun_fan_ins(conn: psycopg.Connection[DictRow]) -> int:
    """Statement 4: both fan-ins again for every consultation either could
    move; returns how many moved.

    `transitions.advance_consultation` takes the row lock, runs both
    guarded UPDATEs and writes the outbox row for whichever fired, so a
    consultation already past both is a lock and nothing else (docs/02,
    section 6). Each consultation commits on its own, so no pass holds
    one consultation's lock while it waits on the next.
    """
    candidates = conn.execute(
        "SELECT id FROM consultation WHERE status IN ('processing', 'awaiting_review') ORDER BY id"
    ).fetchall()
    advanced = 0
    for candidate in candidates:
        advance = transitions.advance_consultation(conn, candidate["id"])
        conn.commit()
        if advance.themes_ready or advance.analysis_ready:
            advanced += 1
    return advanced


def _fake_notify(outbox_id: int) -> str:
    """Notify's reply to a send whose reference is the outbox id (ADR-006):
    a notification id, made up from the reference."""
    return f"fake-notify-{outbox_id}"


def relay(
    conn: psycopg.Connection[DictRow],
    *,
    limit: int = RELAY_LIMIT,
    send: Callable[[int], str] = _fake_notify,
) -> int:
    """Statement 5: send up to `limit` of the emails the outbox owes;
    returns how many went.

    ADR-006's query takes pending rows in id order FOR UPDATE SKIP LOCKED,
    the manual's own suggestion for many consumers of a queue-like table,
    so two relays neither wait on each other nor take the same row. It
    takes one row a transaction here, ADR-006's mark, send, mark: the row
    goes to sending and commits, the send runs with no transaction open,
    and the sent mark, with the reference and the time, commits before
    the next row is taken. So a crash or a failed send strands at most
    the one row it was sending, and every row it hadn't reached is still
    pending for the next pass.

    The send is a stand-in (`_fake_notify`): Notify stays a hint in the
    proof-of-concept (plan section 0).
    """
    sent = 0
    for _ in range(limit):
        taken = conn.execute(
            """
            SELECT id FROM notification_outbox
             WHERE status = 'pending'
             ORDER BY id
               FOR UPDATE SKIP LOCKED
             LIMIT 1
            """
        ).fetchone()
        if taken is None:
            break
        outbox_id: int = taken["id"]
        conn.execute(
            "UPDATE notification_outbox SET status = 'sending' WHERE id = %s", (outbox_id,)
        )
        conn.commit()
        notify_id = send(outbox_id)
        sent += conn.execute(
            """
            UPDATE notification_outbox SET status = 'sent', notify_id = %s, sent_at = now()
             WHERE id = %s AND status = 'sending'
            """,
            (notify_id, outbox_id),
        ).rowcount
        conn.commit()
    return sent


def _fail_each(
    conn: psycopg.Connection[DictRow],
    jobs: Sequence[DictRow],
    recheck: LiteralString,
    params: dict[str, object],
) -> int:
    """`fail_job` on each scanned job, each in a transaction of its own and
    committed; returns how many it failed.

    The scan read no lock, so under the consultation's lock the job is
    read again with the scan's own test, `recheck`, FOR UPDATE SKIP
    LOCKED, and left when no row comes back. That covers a job that has
    moved on since the scan (a fifth attempt that finished, another
    reconciler that failed it first), one whose worker has committed a
    heartbeat since, and one a worker holds right now inside its batch
    transaction. The last two are live leases, and docs/02 section 5 fails
    only "running with a stale heartbeat"; waiting on the held row would
    also keep the consultation locked, and every statement after this one
    waiting, for as long as that worker pauses. The row, once locked here,
    can't move before `fail_job` writes it, and the order is still the
    consultation, then the job (module docstring).
    """
    failed = 0
    for job in jobs:
        transitions.lock_consultation(conn, job["consultation_id"])
        still = conn.execute(recheck, {**params, "job_id": job["id"]}).fetchone()
        if still is not None:
            transitions.fail_job(conn, job["id"])
            failed += 1
        conn.commit()
    return failed
