"""Tags: inserted idempotently, retracted in place, never deleted.

ADR-004. The worker inserts with ON CONFLICT DO NOTHING on the full unique
index (answer, theme, version), so a batch replayed after a takeover adds
nothing, and because the index is full rather than partial on live rows,
the replay lands on a row a person has retracted and leaves it retracted.
A person retracts by setting retracted_at and re-adds by clearing it; the
row is the history. Nothing here commits.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from consult import jobs
from consult.jobs import Lease, LeaseLostError


def _lease_holds(conn: psycopg.Connection[DictRow], lease: Lease) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM job
         WHERE id = %s AND claimed_by = %s AND attempts = %s AND status = 'running'
        """,
        (lease.job_id, lease.worker, lease.fence),
    ).fetchone()
    return row is not None


@dataclass(frozen=True)
class Tag:
    answer_id: int
    theme_id: UUID


def insert_tags(
    conn: psycopg.Connection[DictRow],
    lease: Lease,
    version_id: UUID,
    *,
    batch_no: int,
    tags: Sequence[Tag],
) -> int:
    """Insert one batch's tags; the count is how many were new.

    A pair only goes in if the theme belongs to the version and the answer
    to the version's question and department: a row naming another
    department's answer would be readable under that department's
    row-level security (docs/06, section 2.4), so the insert checks rather
    than trusting the caller to have. The job row is read under the fence
    too, so a stale lease inserts nothing on its own account.
    """
    jobs.heartbeat(conn, lease)
    if not tags:
        return 0
    cursor = conn.execute(
        """
        INSERT INTO answer_theme (department_id, answer_id, theme_id, theme_set_version_id,
                                  job_id, batch_no, source)
        SELECT v.department_id, a.id, t.id, v.id, j.id, %(batch_no)s, 'ai'
          FROM unnest(%(answer_ids)s::bigint[], %(theme_ids)s::uuid[]) AS pair(answer_id, theme_id)
          JOIN theme_set_version v ON v.id = %(version_id)s
          JOIN theme t ON t.id = pair.theme_id AND t.theme_set_version_id = v.id
          JOIN answer a ON a.id = pair.answer_id
                       AND a.question_id = v.question_id AND a.department_id = v.department_id
          JOIN job j ON j.id = %(job_id)s AND j.claimed_by = %(worker)s
                    AND j.attempts = %(fence)s AND j.status = 'running'
        ON CONFLICT (answer_id, theme_id, theme_set_version_id) DO NOTHING
        """,
        {
            "job_id": lease.job_id,
            "worker": lease.worker,
            "fence": lease.fence,
            "batch_no": batch_no,
            "version_id": version_id,
            "answer_ids": [tag.answer_id for tag in tags],
            "theme_ids": [tag.theme_id for tag in tags],
        },
    )
    if cursor.rowcount == 0 and not _lease_holds(conn, lease):
        raise LeaseLostError(lease)
    return cursor.rowcount


def retract(conn: psycopg.Connection[DictRow], tag_id: int, user_id: UUID) -> bool:
    return (
        conn.execute(
            """
            UPDATE answer_theme SET retracted_at = now(), retracted_by = %s
             WHERE id = %s AND retracted_at IS NULL
            """,
            (user_id, tag_id),
        ).rowcount
        == 1
    )


def restore(conn: psycopg.Connection[DictRow], tag_id: int) -> bool:
    return (
        conn.execute(
            """
            UPDATE answer_theme SET retracted_at = NULL, retracted_by = NULL
             WHERE id = %s AND retracted_at IS NOT NULL
            """,
            (tag_id,),
        ).rowcount
        == 1
    )


def add_human_tag(
    conn: psycopg.Connection[DictRow],
    answer_id: int,
    theme_id: UUID,
    version_id: UUID,
    user_id: UUID,
) -> int:
    """A tag a person adds. On the row that already exists, retracted or
    not, this clears the retraction rather than making a second row. The
    same line-up check as the worker's insert; 0 means nothing matched."""
    row = conn.execute(
        """
        INSERT INTO answer_theme (department_id, answer_id, theme_id, theme_set_version_id,
                                  source, user_id)
        SELECT v.department_id, a.id, t.id, v.id, 'human', %(user_id)s
          FROM theme_set_version v
          JOIN theme t ON t.id = %(theme_id)s AND t.theme_set_version_id = v.id
          JOIN answer a ON a.id = %(answer_id)s
                       AND a.question_id = v.question_id AND a.department_id = v.department_id
         WHERE v.id = %(version_id)s
        ON CONFLICT (answer_id, theme_id, theme_set_version_id)
        DO UPDATE SET retracted_at = NULL, retracted_by = NULL
        RETURNING id
        """,
        {
            "answer_id": answer_id,
            "theme_id": theme_id,
            "version_id": version_id,
            "user_id": user_id,
        },
    ).fetchone()
    return int(row["id"]) if row else 0
