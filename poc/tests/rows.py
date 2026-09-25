"""Rows for the tests to hang a job on: a department, a consultation, a job.

Each returns the new row's id and nothing else, and inserts nothing the
design doesn't need for the row to exist.
"""

from __future__ import annotations

from uuid import UUID

import psycopg
from psycopg.abc import Query
from psycopg.rows import DictRow


def _returning_id(
    conn: psycopg.Connection[DictRow], query: Query, params: tuple[object, ...]
) -> UUID:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    value = row["id"]
    assert isinstance(value, UUID)
    return value


def make_department(
    conn: psycopg.Connection[DictRow], name: str = "Department of Fictional Affairs"
) -> UUID:
    return _returning_id(conn, "INSERT INTO department (name) VALUES (%s) RETURNING id", (name,))


def make_consultation(
    conn: psycopg.Connection[DictRow], department_id: UUID, name: str = "Riverside cycle route"
) -> UUID:
    return _returning_id(
        conn,
        """
        INSERT INTO consultation (department_id, name, source, created_by)
        VALUES (%s, %s, 'generic', gen_random_uuid())
        RETURNING id
        """,
        (department_id, name),
    )


def make_running_job(
    conn: psycopg.Connection[DictRow],
    consultation_id: UUID,
    *,
    kind: str = "find_themes",
    claimed_by: str = "worker-1",
    attempts: int = 1,
) -> UUID:
    """A job as a worker holds it after the claim in docs/02 step 5."""
    return _returning_id(
        conn,
        """
        INSERT INTO job (department_id, consultation_id, kind, run_id, status,
                         attempts, claimed_by, heartbeat_at)
        SELECT department_id, id, %s, run_id, 'running', %s, %s, now()
          FROM consultation WHERE id = %s
        RETURNING id
        """,
        (kind, attempts, claimed_by, consultation_id),
    )
