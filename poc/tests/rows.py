"""Rows for the tests to hang a job on: a department, a consultation, a job.

Each returns the new row's id and nothing else, and inserts nothing the
design doesn't need for the row to exist.
"""

from __future__ import annotations

from uuid import UUID

import psycopg
from psycopg.rows import DictRow


def _returning_id(
    conn: psycopg.Connection[DictRow], query: str, params: tuple[object, ...]
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


def _returning_int(
    conn: psycopg.Connection[DictRow], query: str, params: tuple[object, ...]
) -> int:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    value = row["id"]
    assert isinstance(value, int)
    return value


def make_open_question(
    conn: psycopg.Connection[DictRow],
    consultation_id: UUID,
    column_ref: str = "o_reason",
    *,
    status: str = "finding_themes",
    ordinal: int = 1,
) -> UUID:
    """An open question in the state the per-question machine gives it
    while its find_themes job runs (docs/02, section 6)."""
    return _returning_id(
        conn,
        """
        INSERT INTO question (department_id, consultation_id, column_ref, question_text,
                              kind, ordinal, status)
        SELECT department_id, id, %s, 'Why do you feel that way?', 'open', %s, %s
          FROM consultation WHERE id = %s
        RETURNING id
        """,
        (column_ref, ordinal, status, consultation_id),
    )


def make_queued_job(
    conn: psycopg.Connection[DictRow],
    consultation_id: UUID,
    question_id: UUID | None = None,
    *,
    kind: str = "find_themes",
    status: str = "queued",
) -> UUID:
    """A job as dispatch leaves it: queued, sent, not yet claimed (docs/02, step 4)."""
    return _returning_id(
        conn,
        """
        INSERT INTO job (department_id, consultation_id, question_id, kind, run_id, status, sent_at)
        SELECT department_id, id, %s, %s, run_id, %s, now()
          FROM consultation WHERE id = %s
        RETURNING id
        """,
        (question_id, kind, status, consultation_id),
    )


def make_theme_set_version(
    conn: psycopg.Connection[DictRow],
    question_id: UUID,
    *,
    version_no: int = 1,
    status: str = "candidate",
) -> UUID:
    return _returning_id(
        conn,
        """
        INSERT INTO theme_set_version (department_id, question_id, version_no, status)
        SELECT department_id, id, %s, %s FROM question WHERE id = %s
        RETURNING id
        """,
        (version_no, status, question_id),
    )


def make_theme(
    conn: psycopg.Connection[DictRow], version_id: UUID, key: str, label: str | None = None
) -> UUID:
    return _returning_id(
        conn,
        """
        INSERT INTO theme (department_id, theme_set_version_id, key, label)
        SELECT department_id, id, %s, %s FROM theme_set_version WHERE id = %s
        RETURNING id
        """,
        (key, label or key.replace("_", " ").capitalize(), version_id),
    )


def make_respondent(
    conn: psycopg.Connection[DictRow], consultation_id: UUID, source_row_no: int
) -> int:
    return _returning_int(
        conn,
        """
        INSERT INTO respondent (department_id, consultation_id, source_row_no)
        SELECT department_id, id, %s FROM consultation WHERE id = %s
        RETURNING id
        """,
        (source_row_no, consultation_id),
    )


def make_answer(
    conn: psycopg.Connection[DictRow],
    consultation_id: UUID,
    respondent_id: int,
    question_id: UUID,
    text: str,
) -> int:
    return _returning_int(
        conn,
        """
        INSERT INTO answer (department_id, consultation_id, respondent_id, question_id, value_text)
        SELECT department_id, id, %s, %s, %s FROM consultation WHERE id = %s
        RETURNING id
        """,
        (respondent_id, question_id, text, consultation_id),
    )
