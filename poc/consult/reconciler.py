"""The reconciler's statements (docs/02, section 5).

Every five minutes, in order, each idempotent, and each its own
transaction or run of transactions: a second pass over what the first
left changes nothing. The job table is the truth and a message is a hint
(ADR-006, last paragraph), so the proof-of-concept's "re-send" is a fresh
`sent_at` and nothing more: nothing here claims, and a worker's
conditional claim is still what decides who runs a job (docs/02, step 5).

Lock order is `transitions.fail_job`'s: the consultation row, then the
job row, which is the order a worker's finishing transaction takes them
in too. So a statement never holds a job row lock when it calls
`fail_job`: its bulk UPDATE commits first, and each job at the retry
budget is then failed in a transaction of its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

import psycopg
from psycopg.rows import DictRow

from consult import transitions
from consult.jobs import STALE_AFTER
from consult.worker import MAX_ATTEMPTS

# The states fail_job fails a job from; anything else has moved on since
# the scan that found it (see _fail_each).
_FAILABLE = frozenset({"queued", "running", "failed_retryable"})


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

    The re-send is one UPDATE, committed before any job is failed, so no
    job row lock is held when `fail_job` takes the consultation's (module
    docstring).
    """
    params = {"stale_after": stale_after, "max_attempts": max_attempts}
    resent = conn.execute(
        """
        UPDATE job SET sent_at = now()
         WHERE status IN ('queued', 'running') AND attempts < %(max_attempts)s
           AND CASE status WHEN 'queued' THEN sent_at ELSE heartbeat_at END
               < now() - %(stale_after)s
           AND sent_at < now() - %(stale_after)s
        """,
        params,
    ).rowcount
    conn.commit()
    spent = conn.execute(
        """
        SELECT id, consultation_id FROM job
         WHERE status IN ('queued', 'running') AND attempts >= %(max_attempts)s
           AND CASE status WHEN 'queued' THEN sent_at ELSE heartbeat_at END
               < now() - %(stale_after)s
         ORDER BY id
        """,
        params,
    ).fetchall()
    return resent, _fail_each(conn, spent)


def _fail_each(conn: psycopg.Connection[DictRow], jobs: Sequence[DictRow]) -> int:
    """`fail_job` on each scanned job, each in a transaction of its own and
    committed; returns how many it failed.

    The job's status is read again under the consultation's lock, because
    the job may have moved since the scan: a fifth attempt that finished
    after all, or another reconciler that failed it first. Neither can
    land between that read and `fail_job`, since a job only succeeds in
    `finish_find_themes` or `finish_map_themes`, under the same lock, and
    `fail_job` takes it too.
    """
    failed = 0
    for job in jobs:
        transitions.lock_consultation(conn, job["consultation_id"])
        current = conn.execute("SELECT status FROM job WHERE id = %s", (job["id"],)).fetchone()
        if current is not None and current["status"] in _FAILABLE:
            transitions.fail_job(conn, job["id"])
            failed += 1
        conn.commit()
    return failed
