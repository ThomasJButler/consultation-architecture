"""Claiming, heartbeating and checkpointing a job.

The unit of work is one job row (ADR-002). A worker claims it with one
conditional UPDATE and the number that comes back, `attempts`, is the
fence: every later write the worker makes carries it, and a write whose
fence is stale does nothing, so a worker that was taken over after its
heartbeat went quiet can't spend or corrupt once it wakes up (docs/02,
step 5). Nothing here commits; the caller owns the transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from consult.errors import ErrorCode

# Ten minutes of silence and a lease can be taken over (docs/02, step 5;
# ADR-002 says why not shorter: a false takeover costs a batch).
STALE_AFTER = timedelta(minutes=10)


@dataclass(frozen=True)
class Lease:
    """What a worker holds after a claim: the job, its own name, the fence."""

    job_id: UUID
    worker: str
    fence: int


def claim(
    conn: psycopg.Connection[DictRow],
    job_id: UUID,
    worker: str,
    *,
    stale_after: timedelta = STALE_AFTER,
) -> Lease | None:
    """Take the job if it's queued or its lease has gone stale.

    None means one of three things (a live lease, already finished, an
    unknown id); the worker logs which and drops the message.
    """
    row = conn.execute(
        """
        UPDATE job
           SET status = 'running', attempts = attempts + 1,
               claimed_by = %(worker)s, heartbeat_at = now()
         WHERE id = %(job_id)s
           AND (status = 'queued'
                OR (status = 'running' AND heartbeat_at < now() - %(stale_after)s))
        RETURNING attempts
        """,
        {"job_id": job_id, "worker": worker, "stale_after": stale_after},
    ).fetchone()
    if row is None:
        return None
    return Lease(job_id, worker, row["attempts"])


class LeaseLostError(Exception):
    """A write with a stale fence: another worker holds the lease now. The
    worker stops at the next batch boundary and writes nothing more."""

    code = ErrorCode.LEASE_LOST

    def __init__(self, lease: Lease) -> None:
        self.lease = lease
        super().__init__(f"{self.code.value} job={lease.job_id} fence={lease.fence}")


def _fenced(
    conn: psycopg.Connection[DictRow], lease: Lease, sql: str, params: dict[str, object]
) -> None:
    """Run a job update under the fence and refuse it if the lease has gone."""
    cursor = conn.execute(
        sql,
        {**params, "job_id": lease.job_id, "worker": lease.worker, "fence": lease.fence},
    )
    if cursor.rowcount != 1:
        raise LeaseLostError(lease)


def heartbeat(conn: psycopg.Connection[DictRow], lease: Lease) -> None:
    """The write every later write starts with (docs/02, step 5)."""
    _fenced(
        conn,
        lease,
        """
        UPDATE job SET heartbeat_at = now()
         WHERE id = %(job_id)s AND claimed_by = %(worker)s
           AND attempts = %(fence)s AND status = 'running'
        """,
        {},
    )


def checkpoint(
    conn: psycopg.Connection[DictRow],
    lease: Lease,
    *,
    batch_no: int,
    stage: str,
    answer_ids: Sequence[int],
    status: str = "done",
    trace_id: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> bool:
    """Record one batch. False means it was already there, which is what a
    replay after takeover looks like (ADR-002).

    The INSERT selects from the job row under the fence as well, so it
    refuses a stale lease on its own and not only through the heartbeat's
    row lock, which lasts only as long as the caller's transaction.
    """
    heartbeat(conn, lease)
    cursor = conn.execute(
        """
        INSERT INTO job_batch (department_id, job_id, batch_no, stage, answer_ids, status,
                               trace_id, tokens_in, tokens_out)
        SELECT department_id, id, %(batch_no)s, %(stage)s, %(answer_ids)s, %(status)s,
               %(trace_id)s, %(tokens_in)s, %(tokens_out)s
          FROM job
         WHERE id = %(job_id)s AND claimed_by = %(worker)s
           AND attempts = %(fence)s AND status = 'running'
        ON CONFLICT (job_id, batch_no) DO NOTHING
        """,
        {
            "job_id": lease.job_id,
            "worker": lease.worker,
            "fence": lease.fence,
            "batch_no": batch_no,
            "stage": stage,
            "answer_ids": list(answer_ids),
            "status": status,
            "trace_id": trace_id,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        },
    )
    if cursor.rowcount == 1:
        return True
    # Zero rows is either a replay (the batch is already there) or a lost
    # lease (the SELECT found no job); tell them apart before answering.
    existing = conn.execute(
        "SELECT 1 FROM job_batch WHERE job_id = %s AND batch_no = %s", (lease.job_id, batch_no)
    ).fetchone()
    if existing is None:
        raise LeaseLostError(lease)
    return False


def record_failure(
    conn: psycopg.Connection[DictRow],
    lease: Lease,
    error_code: ErrorCode,
    *,
    provider_request_id: str | None = None,
    retry_in: timedelta = timedelta(minutes=1),
) -> None:
    """Record a failure as a code and a request id, never a message.

    The job goes to failed_retryable with a time to retry; whether it
    retries or is marked failed at five attempts is the reconciler's call
    (docs/02, section 5). Fenced like every other write.
    """
    _fenced(
        conn,
        lease,
        """
        UPDATE job
           SET status = 'failed_retryable',
               error_code = %(error_code)s,
               provider_request_id = %(provider_request_id)s,
               next_attempt_at = now() + %(retry_in)s,
               heartbeat_at = now()
         WHERE id = %(job_id)s AND claimed_by = %(worker)s
           AND attempts = %(fence)s AND status = 'running'
        """,
        {
            "error_code": error_code.value,
            "provider_request_id": provider_request_id,
            "retry_in": retry_in,
        },
    )


def next_batch_no(conn: psycopg.Connection[DictRow], job_id: UUID) -> int:
    """Where a worker starts: after the last checkpoint, or at 1."""
    row = conn.execute(
        "SELECT coalesce(max(batch_no), 0) + 1 AS next FROM job_batch WHERE job_id = %s", (job_id,)
    ).fetchone()
    # coalesce makes the aggregate one row always; the guard is for the type.
    return int(row["next"]) if row else 1


def succeed(conn: psycopg.Connection[DictRow], lease: Lease) -> None:
    """The job's last write, fenced like the rest."""
    _fenced(
        conn,
        lease,
        """
        UPDATE job SET status = 'succeeded', heartbeat_at = now()
         WHERE id = %(job_id)s AND claimed_by = %(worker)s
           AND attempts = %(fence)s AND status = 'running'
        """,
        {},
    )


def queue(conn: psycopg.Connection[DictRow], job_id: UUID, *, model_alias: str, seed: int) -> bool:
    """pending to queued, with the alias and the seed the runner reads. A
    stand-in for dispatch: docs/02 step 4 dispatches under the caps and
    sends a message, and PR-08 builds that; until then a command queues the
    one job it is about to run. False when the job isn't pending."""
    moved = conn.execute(
        """
        UPDATE job
           SET status = 'queued', sent_at = now(), model_alias = %(alias)s,
               params = params || jsonb_build_object('seed', %(seed)s::int)
         WHERE id = %(job_id)s AND status = 'pending'
        """,
        {"job_id": job_id, "alias": model_alias, "seed": seed},
    ).rowcount
    return moved == 1
