"""The pure pick under the three caps (docs/02, step 4).

Caps: six jobs a department, four a consultation, twenty service-wide,
round-robin across departments when contended. `select` only ranks the
pending jobs against those caps and the live (queued plus running) counts;
it does no I/O, which is why the ranking runs here in Python and not as a
SQL window function: the hard part of dispatch is then testable without
Docker. `dispatch(conn, settings)` calls it inside a transaction and
turns its answer into one locked `UPDATE`, guarded on `status = 'pending'`
so the pick can only be applied once. That guard is the whole of what the
`UPDATE` decides; the advisory lock below is what stops two dispatchers
reading the same free slots and filling them both.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from consult.config import Settings

# One key for every dispatch, so dispatches take turns. The inserting
# transaction and the reconciler can dispatch at once, and each reads the
# live counts before the other commits, so both would see the same free
# slots and fill them twice over (docs/02, step 4). A transaction-level
# advisory lock is let go at commit or rollback (PostgreSQL 17 manual,
# 13.3.5), so the next dispatcher reads the counts this one's UPDATE left.
# The number is "consult" in ASCII, picked only to be a fixed bigint.
DISPATCH_LOCK = 0x636F6E73756C74


@dataclass(frozen=True)
class Pending:
    """One pending job as dispatch reads it."""

    job_id: UUID
    department_id: UUID
    consultation_id: UUID
    created_at: datetime


@dataclass(frozen=True)
class LiveCounts:
    """Queued plus running jobs, the slots the caps already count as taken."""

    per_department: Mapping[UUID, int]
    per_consultation: Mapping[UUID, int]
    in_all: int


@dataclass(frozen=True)
class JobCaps:
    """The three caps docs/02 step 4 sets."""

    per_department: Mapping[UUID, int]  # department.concurrent_jobs_cap, per department id
    per_consultation: int  # CONSULT_JOBS_PER_CONSULTATION
    in_all: int  # CONSULT_JOBS_IN_ALL


def select(pending: Sequence[Pending], live: LiveCounts, caps: JobCaps) -> list[UUID]:
    """The ids to queue, in the order they'd be queued."""
    ordered = sorted(pending, key=lambda job: (job.created_at, job.job_id))

    queues: dict[UUID, list[Pending]] = {}
    for job in ordered:
        queues.setdefault(job.department_id, []).append(job)

    # Round order: each department's turn comes round in the order its
    # oldest pending job arrived (docs/02 step 4). `queues[d]` is already
    # created_at, id ordered, so its first entry is that department's
    # oldest. A department missing from caps.per_department wasn't read
    # by the caller, so it gets nothing rather than a guessed cap.
    rotation = [
        department_id
        for department_id in sorted(
            queues, key=lambda d: (queues[d][0].created_at, queues[d][0].job_id)
        )
        if department_id in caps.per_department
    ]
    cursor = dict.fromkeys(rotation, 0)
    department_taken = {
        department_id: live.per_department.get(department_id, 0) for department_id in rotation
    }
    consultation_taken: dict[UUID, int] = dict(live.per_consultation)
    taken = live.in_all

    picked: list[UUID] = []
    while rotation and taken < caps.in_all:
        placed_this_round = False
        for department_id in list(rotation):
            if taken >= caps.in_all:
                break
            if department_taken[department_id] >= caps.per_department[department_id]:
                rotation.remove(department_id)
                continue
            queue = queues[department_id]
            index = cursor[department_id]
            # A job blocked only by its consultation's cap is skipped and
            # the next job of the same department tried (docs/02 step 4).
            while (
                index < len(queue)
                and consultation_taken.get(queue[index].consultation_id, 0) >= caps.per_consultation
            ):
                index += 1
            if index == len(queue):
                cursor[department_id] = index
                rotation.remove(department_id)
                continue
            job = queue[index]
            picked.append(job.job_id)
            department_taken[department_id] += 1
            consultation_taken[job.consultation_id] = (
                consultation_taken.get(job.consultation_id, 0) + 1
            )
            taken += 1
            cursor[department_id] = index + 1
            placed_this_round = True
        if not placed_this_round:
            break

    return picked


def dispatch(conn: psycopg.Connection[DictRow], settings: Settings) -> int:
    """Queue what `select` picks from the pending find_themes and map_themes
    jobs, in the caller's transaction, and return how many went.

    Only these two kinds: a stage or an ingest job would take a cap slot
    and hand the worker something it has no runner for, so neither gets a
    job row to dispatch (transitions.start_staging;
    plans/PR-08-poc-mapping-worker.md, section 0).

    A job with no alias or seed gets the settings' alias and a fresh seed; a
    retried job keeps both, so the plan its checkpoints were cut from can be
    rebuilt (ADR-002). The caller commits, then sends (docs/02, step 4).
    """
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (DISPATCH_LOCK,))
    pending = [
        Pending(
            job_id=row["id"],
            department_id=row["department_id"],
            consultation_id=row["consultation_id"],
            created_at=row["created_at"],
        )
        for row in conn.execute(
            """
            SELECT id, department_id, consultation_id, created_at FROM job
             WHERE status = 'pending' AND kind IN ('find_themes', 'map_themes')
             ORDER BY created_at, id
            """
        ).fetchall()
    ]
    per_department: dict[UUID, int] = {}
    per_consultation: dict[UUID, int] = {}
    for row in conn.execute(
        """
        SELECT department_id, consultation_id, count(*) AS live FROM job
         WHERE status IN ('queued', 'running')
         GROUP BY department_id, consultation_id
        """
    ).fetchall():
        department_id = row["department_id"]
        per_department[department_id] = per_department.get(department_id, 0) + row["live"]
        per_consultation[row["consultation_id"]] = row["live"]
    department_caps = {
        row["id"]: row["concurrent_jobs_cap"]
        for row in conn.execute("SELECT id, concurrent_jobs_cap FROM department").fetchall()
    }

    picked = select(
        pending,
        LiveCounts(per_department, per_consultation, sum(per_consultation.values())),
        JobCaps(department_caps, settings.jobs_per_consultation, settings.jobs_in_all),
    )
    # One seed per job, below 2**31 to fit the int[] the UPDATE casts to.
    seeds = [secrets.randbelow(2**31) for _ in picked]
    return conn.execute(
        """
        UPDATE job
           SET status = 'queued', sent_at = now(),
               model_alias = coalesce(job.model_alias, %(alias)s),
               params = CASE WHEN job.params ? 'seed' THEN job.params
                             ELSE job.params || jsonb_build_object('seed', picked.seed) END
          FROM unnest(%(ids)s::uuid[], %(seeds)s::int[]) AS picked (id, seed)
         WHERE job.id = picked.id AND job.status = 'pending'
        """,
        {"alias": settings.model_alias, "ids": picked, "seeds": seeds},
    ).rowcount
