"""Claiming, heartbeating and checkpointing a job.

The unit of work is one job row (ADR-002). A worker claims it with one
conditional UPDATE and the number that comes back, `attempts`, is the
fence: every later write the worker makes carries it, and a write whose
fence is stale does nothing, so a worker that was taken over after its
heartbeat went quiet can't spend or corrupt once it wakes up (docs/02,
step 5). Nothing here commits; the caller owns the transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

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
