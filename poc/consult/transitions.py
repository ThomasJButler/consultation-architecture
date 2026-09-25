"""The transitions: every one a transaction, and one routine for the
consultation's status.

docs/02 section 2 in code. A worker finishing a question locks the
consultation row first, in its own statement, then moves the question on,
then calls advance_consultation, which runs both guarded UPDATEs and
writes the outbox row for whichever fired, all in the caller's
transaction. Section 6 says there is no second way to change the column,
and a repo rule pins that.

Why the lock comes first: under READ COMMITTED a blocked UPDATE
re-evaluates only its own WHERE clause against the row it blocked on and
"does not see effects of those commands on other rows" (PostgreSQL 17
manual, 13.2.1, in docs/01). Two finishers each see the other's question
still running and neither flips. The row lock serialises them, so the
second finisher's NOT EXISTS runs after the first has committed.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from consult import jobs
from consult.jobs import Lease


class TransitionError(Exception):
    """A guarded transition found the row in another state than the one
    it expected: a question already moved on, or a consultation reopened."""


@dataclass(frozen=True)
class Advance:
    themes_ready: bool
    analysis_ready: bool


def lock_consultation(conn: psycopg.Connection[DictRow], consultation_id: UUID) -> None:
    conn.execute("SELECT id FROM consultation WHERE id = %s FOR UPDATE", (consultation_id,))


def _outbox(conn: psycopg.Connection[DictRow], consultation_id: UUID, kind: str) -> None:
    # Read the pass id from the locked row, so a reopen's new run_id is the
    # one the row carries (docs/04, section 2). ON CONFLICT is the second
    # half of "exactly one email per milestone per pass" (ADR-006).
    conn.execute(
        """
        INSERT INTO notification_outbox (department_id, consultation_id, kind, subject_id)
        SELECT department_id, id, %(kind)s, run_id FROM consultation WHERE id = %(id)s
        ON CONFLICT DO NOTHING
        """,
        {"id": consultation_id, "kind": kind},
    )


def advance_consultation(conn: psycopg.Connection[DictRow], consultation_id: UUID) -> Advance:
    """Take the row lock, run both guarded UPDATEs, write the outbox row for
    whichever fired. The worker's completing transaction calls it; so will
    the reconciler's fourth statement, a reopen and an operator retry."""
    lock_consultation(conn, consultation_id)
    # The predicate is a positive list of the states a question hasn't got
    # past yet, so a question a quick reviewer has already signed off
    # doesn't hold the email back, and a failed one doesn't either: four
    # ready questions shouldn't wait on an operator (docs/02, step 7).
    themes_ready = (
        conn.execute(
            """
            UPDATE consultation
               SET status = 'awaiting_review', status_changed_at = now(), awaiting_review_at = now()
             WHERE id = %(id)s AND status = 'processing'
               AND NOT EXISTS (
                     SELECT 1 FROM question
                      WHERE consultation_id = %(id)s AND kind = 'open'
                        AND status IN ('configured', 'finding_themes'))
            """,
            {"id": consultation_id},
        ).rowcount
        == 1
    )
    if themes_ready:
        _outbox(conn, consultation_id, "themes_ready")
    # The second milestone needs every open question complete: a failed
    # mapping blocks ready by design (docs/02, step 10).
    analysis_ready = (
        conn.execute(
            """
            UPDATE consultation
               SET status = 'ready', status_changed_at = now()
             WHERE id = %(id)s AND status = 'awaiting_review'
               AND NOT EXISTS (
                     SELECT 1 FROM question
                      WHERE consultation_id = %(id)s AND kind = 'open' AND status <> 'complete')
            """,
            {"id": consultation_id},
        ).rowcount
        == 1
    )
    if analysis_ready:
        _outbox(conn, consultation_id, "analysis_ready")
    return Advance(themes_ready, analysis_ready)


def _move_question(
    conn: psycopg.Connection[DictRow], question_id: UUID, from_status: str, to_status: str
) -> None:
    moved = conn.execute(
        "UPDATE question SET status = %s WHERE id = %s AND status = %s",
        (to_status, question_id, from_status),
    ).rowcount
    if moved != 1:
        raise TransitionError(f"question {question_id} is not {from_status}")


def finish_find_themes(
    conn: psycopg.Connection[DictRow], lease: Lease, question_id: UUID, consultation_id: UUID
) -> Advance:
    """The end of step 6 and the whole of step 7, in the caller's transaction.

    The theme set version and its themes go in before this is called
    (PR-07); this moves the question on, runs the fan-in and marks the job.
    """
    lock_consultation(conn, consultation_id)
    jobs.heartbeat(conn, lease)
    _move_question(conn, question_id, "finding_themes", "themes_ready")
    advance = advance_consultation(conn, consultation_id)
    jobs.succeed(conn, lease)
    return advance


def finish_map_themes(
    conn: psycopg.Connection[DictRow], lease: Lease, question_id: UUID, consultation_id: UUID
) -> Advance:
    """The end of step 9 and the whole of step 10: same shape as the first,
    with the other predicate inside advance_consultation."""
    lock_consultation(conn, consultation_id)
    jobs.heartbeat(conn, lease)
    _move_question(conn, question_id, "assigning_themes", "complete")
    advance = advance_consultation(conn, consultation_id)
    jobs.succeed(conn, lease)
    return advance


@dataclass(frozen=True)
class SignOff:
    version_id: UUID
    job_id: UUID


FALLBACK_THEMES = (("OTHER", "Other"), ("NO_REASON", "No reason given"))


def sign_off(
    conn: psycopg.Connection[DictRow], question_id: UUID, reviewer: UUID
) -> SignOff | None:
    """Confirm the themes for one question (docs/02, step 8; ADR-003).

    The guard is the mutex: two reviewers clicking at once produce one
    signed-off question and one None. In the same transaction the
    candidate is frozen as the next version with stable keys plus OTHER
    and NO_REASON, the candidate is superseded, and a map_themes job for
    this question only is inserted under job_one_per_run.
    """
    confirmed = conn.execute(
        "UPDATE question SET status = 'signed_off' WHERE id = %s AND status = 'themes_ready'",
        (question_id,),
    ).rowcount
    if confirmed != 1:
        return None
    candidate = conn.execute(
        """
        SELECT id FROM theme_set_version
         WHERE question_id = %s AND status = 'candidate'
         ORDER BY version_no DESC LIMIT 1
        """,
        (question_id,),
    ).fetchone()
    if candidate is None:
        raise TransitionError(f"question {question_id} has no candidate version to sign off")
    frozen = conn.execute(
        """
        INSERT INTO theme_set_version (department_id, question_id, version_no, status,
                                       parent_version_id, signed_off_by, signed_off_at)
        SELECT department_id, question_id, version_no + 1, 'signed_off', id, %(reviewer)s, now()
          FROM theme_set_version WHERE id = %(candidate)s
        RETURNING id
        """,
        {"candidate": candidate["id"], "reviewer": reviewer},
    ).fetchone()
    if frozen is None:
        raise TransitionError(f"question {question_id}: the signed-off version was not written")
    version_id: UUID = frozen["id"]
    # Keys stay stable across versions; lineage points back at the candidate.
    conn.execute(
        """
        INSERT INTO theme (department_id, theme_set_version_id, key, label, description,
                           is_longlist, is_fallback, lineage_theme_id, preview_count)
        SELECT department_id, %(version)s, key, label, description,
               is_longlist, is_fallback, id, preview_count
          FROM theme WHERE theme_set_version_id = %(candidate)s
        """,
        {"version": version_id, "candidate": candidate["id"]},
    )
    for key, label in FALLBACK_THEMES:
        conn.execute(
            """
            INSERT INTO theme (department_id, theme_set_version_id, key, label, is_fallback)
            SELECT department_id, id, %(key)s, %(label)s, true
              FROM theme_set_version WHERE id = %(version)s
            ON CONFLICT (theme_set_version_id, key) DO NOTHING
            """,
            {"version": version_id, "key": key, "label": label},
        )
    conn.execute(
        "UPDATE theme_set_version SET status = 'superseded' WHERE id = %s", (candidate["id"],)
    )
    job = conn.execute(
        """
        INSERT INTO job (department_id, consultation_id, question_id, kind, run_id, status)
        SELECT q.department_id, q.consultation_id, q.id, 'map_themes', c.run_id, 'pending'
          FROM question q JOIN consultation c ON c.id = q.consultation_id
         WHERE q.id = %s
        RETURNING id
        """,
        (question_id,),
    ).fetchone()
    if job is None:
        raise TransitionError(f"question {question_id}: the map_themes job was not written")
    return SignOff(version_id, job["id"])


@dataclass(frozen=True)
class Reopen:
    run_id: UUID
    candidate_version_id: UUID


def reopen_for_correction(
    conn: psycopg.Connection[DictRow], question_id: UUID, reviewer: UUID
) -> Reopen:
    """Reopen one question's themes for correction (docs/04, section 2).

    Under the row lock: the consultation goes ready to awaiting_review with
    a new run_id, so the pass's later email has a row of its own; the
    question goes complete to themes_ready; a new candidate version is cut
    from the signed-off one, with lineage, for the reviewer to edit.
    """
    owner = conn.execute(
        "SELECT consultation_id FROM question WHERE id = %s", (question_id,)
    ).fetchone()
    if owner is None:
        raise TransitionError(f"question {question_id} does not exist")
    consultation_id: UUID = owner["consultation_id"]
    lock_consultation(conn, consultation_id)
    minted = conn.execute(
        """
        UPDATE consultation
           SET status = 'awaiting_review', run_id = gen_random_uuid(),
               status_changed_at = now(), awaiting_review_at = now()
         WHERE id = %s AND status = 'ready'
        RETURNING run_id
        """,
        (consultation_id,),
    ).fetchone()
    if minted is None:
        raise TransitionError(f"consultation {consultation_id} is not ready")
    _move_question(conn, question_id, "complete", "themes_ready")
    signed = conn.execute(
        """
        SELECT id FROM theme_set_version
         WHERE question_id = %s AND status = 'signed_off'
         ORDER BY version_no DESC LIMIT 1
        """,
        (question_id,),
    ).fetchone()
    if signed is None:
        raise TransitionError(f"question {question_id} has no signed-off version to reopen")
    candidate = conn.execute(
        """
        INSERT INTO theme_set_version (department_id, question_id, version_no, status, parent_version_id)
        SELECT department_id, question_id,
               (SELECT max(version_no) + 1 FROM theme_set_version WHERE question_id = %(question)s),
               'candidate', id
          FROM theme_set_version WHERE id = %(signed)s
        RETURNING id
        """,
        {"question": question_id, "signed": signed["id"]},
    ).fetchone()
    if candidate is None:
        raise TransitionError(f"question {question_id}: the new candidate was not written")
    conn.execute(
        """
        INSERT INTO theme (department_id, theme_set_version_id, key, label, description,
                           is_longlist, is_fallback, lineage_theme_id, preview_count)
        SELECT department_id, %(candidate)s, key, label, description,
               is_longlist, is_fallback, id, preview_count
          FROM theme WHERE theme_set_version_id = %(signed)s
        """,
        {"candidate": candidate["id"], "signed": signed["id"]},
    )
    return Reopen(minted["run_id"], candidate["id"])
