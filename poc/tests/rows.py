"""Rows for the tests to hang a job on: a department, a consultation, a job.

Each returns the new row's id and nothing else, and inserts nothing the
design doesn't need for the row to exist.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb


def _returning_id(
    conn: psycopg.Connection[DictRow], query: str, params: tuple[object, ...]
) -> UUID:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    value = row["id"]
    assert isinstance(value, UUID)
    return value


def make_department(conn: psycopg.Connection[DictRow], name: str | None = None) -> UUID:
    # A fresh name each call: the name is unique, and a test that stages
    # two consultations wants two departments, not a conflict.
    name = name or f"Department of Fictional Affairs {uuid4().hex[:6]}"
    return _returning_id(conn, "INSERT INTO department (name) VALUES (%s) RETURNING id", (name,))


def make_consultation(
    conn: psycopg.Connection[DictRow],
    department_id: UUID,
    name: str = "Riverside cycle route",
    *,
    status: str = "draft",
) -> UUID:
    return _returning_id(
        conn,
        """
        INSERT INTO consultation (department_id, name, source, created_by, status)
        VALUES (%s, %s, 'generic', gen_random_uuid(), %s)
        RETURNING id
        """,
        (department_id, name, status),
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


def make_pending_job(
    conn: psycopg.Connection[DictRow],
    consultation_id: UUID,
    question_id: UUID | None = None,
    *,
    kind: str = "find_themes",
    model_alias: str | None = None,
    seed: int | None = None,
) -> UUID:
    """A job as ingest or sign-off inserts it, waiting for dispatch (docs/02,
    step 4). An alias and a seed make it a retried job back at pending with
    what its first dispatch stamped (ADR-002)."""
    params: dict[str, object] = {} if seed is None else {"seed": seed}
    return _returning_id(
        conn,
        """
        INSERT INTO job (department_id, consultation_id, question_id, kind, run_id,
                         model_alias, params)
        SELECT department_id, id, %s, %s, run_id, %s, %s
          FROM consultation WHERE id = %s
        RETURNING id
        """,
        (question_id, kind, model_alias, Jsonb(params), consultation_id),
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


def tag_answers_by_rule(
    conn: psycopg.Connection[DictRow],
    version_id: UUID,
    question_id: UUID,
    key_for: Callable[[str], str],
    *,
    source: str = "ai",
) -> dict[int, str]:
    """Tag every non-blank answer to `question_id` in `answer_theme` against
    `version_id`, the key chosen by `key_for(value_text)`. A test's stand-in
    for `tags.insert_tags`, which needs a claimed lease this factory has no
    job to hold; the tags a test then counts come from the test and not
    from the worker. Every physical row is tagged, its
    duplicates included, so revealing them with `with=duplicates` changes a
    theme's count and not just the denominator (docs/04 section 6). A key
    `key_for` returns that the version doesn't already carry is created;
    OTHER and NO_REASON usually already are, from `transitions.sign_off`'s
    fallback themes, so this reuses those rather than colliding with them.

    Returns the answer id each key was chosen for, so a test's hand count
    from the fixture CSV can be checked against exactly what was inserted.
    """
    chosen: dict[int, str] = {}
    for row in conn.execute(
        "SELECT id, value_text FROM answer WHERE question_id = %s AND NOT is_blank",
        (question_id,),
    ).fetchall():
        answer_id = row["id"]
        assert isinstance(answer_id, int)
        text = row["value_text"]
        assert isinstance(text, str)
        chosen[answer_id] = key_for(text)
    if not chosen:
        return chosen

    keys = sorted(set(chosen.values()))
    for key in keys:
        conn.execute(
            """
            INSERT INTO theme (department_id, theme_set_version_id, key, label)
            SELECT department_id, id, %(key)s, %(label)s FROM theme_set_version WHERE id = %(version)s
            ON CONFLICT (theme_set_version_id, key) DO NOTHING
            """,
            {"version": version_id, "key": key, "label": key.replace("_", " ").capitalize()},
        )
    theme_ids: dict[str, UUID] = {}
    for row in conn.execute(
        "SELECT id, key FROM theme WHERE theme_set_version_id = %s AND key = ANY(%s)",
        (version_id, keys),
    ).fetchall():
        theme_key = row["key"]
        theme_id = row["id"]
        assert isinstance(theme_key, str)
        assert isinstance(theme_id, UUID)
        theme_ids[theme_key] = theme_id

    answer_ids = list(chosen)
    conn.execute(
        """
        INSERT INTO answer_theme (department_id, answer_id, theme_id, theme_set_version_id, source)
        SELECT v.department_id, pair.answer_id, pair.theme_id, v.id, %(source)s
          FROM unnest(%(answer_ids)s::bigint[], %(theme_ids)s::uuid[]) AS pair(answer_id, theme_id)
          JOIN theme_set_version v ON v.id = %(version)s
        """,
        {
            "version": version_id,
            "source": source,
            "answer_ids": answer_ids,
            "theme_ids": [theme_ids[chosen[answer_id]] for answer_id in answer_ids],
        },
    )
    return chosen


def make_job_batch(
    conn: psycopg.Connection[DictRow],
    job_id: UUID,
    answer_ids: list[int],
    *,
    batch_no: int = 1,
    stage: str = "map_themes",
    status: str = "done",
) -> None:
    """A worker's checkpoint as mapping.py writes one (ADR-002): the batch
    of answers a stage covered, and whether it tagged them or gave up."""
    conn.execute(
        """
        INSERT INTO job_batch (department_id, job_id, batch_no, stage, answer_ids, status)
        SELECT department_id, id, %s, %s, %s, %s FROM job WHERE id = %s
        """,
        (batch_no, stage, answer_ids, status, job_id),
    )


def make_outbox_row(
    conn: psycopg.Connection[DictRow],
    consultation_id: UUID,
    *,
    kind: str = "attention_needed",
    subject_id: UUID | None = None,
) -> int:
    """An email owed, as a transition writes it: pending, naming its subject
    (ADR-006; docs/02, correction 3). A fresh subject when none is given,
    so two rows of one kind don't meet on the key."""
    return _returning_int(
        conn,
        """
        INSERT INTO notification_outbox (department_id, consultation_id, kind, subject_id)
        SELECT department_id, id, %s, %s FROM consultation WHERE id = %s
        RETURNING id
        """,
        (kind, subject_id or uuid4(), consultation_id),
    )
