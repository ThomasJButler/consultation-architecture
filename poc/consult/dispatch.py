"""The pure pick under the three caps (docs/02, step 4).

Caps: six jobs a department, four a consultation, twenty service-wide,
round-robin across departments when contended. `select` only ranks the
pending jobs against those caps and the live (queued plus running) counts;
it does no I/O, so the hard part of dispatch is testable without Docker.
`dispatch(conn, settings)` calls it inside a transaction and turns its
answer into one locked `UPDATE`.

The ranking runs here and not as a SQL window function because Postgres
refuses `FOR UPDATE` in the same query as one (plan section 2, PR-08), and
turning a pick into a queue needs the picked rows locked while they're
claimed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


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
