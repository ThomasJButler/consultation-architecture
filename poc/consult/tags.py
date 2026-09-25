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
from consult.jobs import Lease


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
    """Insert one batch's tags; the count is how many were new."""
    jobs.heartbeat(conn, lease)
    if not tags:
        return 0
    cursor = conn.execute(
        """
        INSERT INTO answer_theme (department_id, answer_id, theme_id, theme_set_version_id,
                                  job_id, batch_no, source)
        SELECT v.department_id, pair.answer_id, pair.theme_id, v.id, %(job_id)s, %(batch_no)s, 'ai'
          FROM theme_set_version v,
               unnest(%(answer_ids)s::bigint[], %(theme_ids)s::uuid[]) AS pair(answer_id, theme_id)
         WHERE v.id = %(version_id)s
        ON CONFLICT (answer_id, theme_id, theme_set_version_id) DO NOTHING
        """,
        {
            "job_id": lease.job_id,
            "batch_no": batch_no,
            "version_id": version_id,
            "answer_ids": [tag.answer_id for tag in tags],
            "theme_ids": [tag.theme_id for tag in tags],
        },
    )
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
    not, this clears the retraction rather than making a second row."""
    row = conn.execute(
        """
        INSERT INTO answer_theme (department_id, answer_id, theme_id, theme_set_version_id,
                                  source, user_id)
        SELECT department_id, %(answer_id)s, %(theme_id)s, id, 'human', %(user_id)s
          FROM theme_set_version WHERE id = %(version_id)s
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
