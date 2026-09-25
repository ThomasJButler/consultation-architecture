"""What the fan-ins promise (docs/02, steps 7 and 10; ADR-001; ADR-006).

A worker finishing a question locks the consultation row, moves the
question on, checks whether every open question has reached the
milestone, flips the consultation if so and writes the email row, in one
commit. One routine, advance_consultation, is the only writer of the
consultation's status.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import DictRow

from consult.jobs import claim
from consult.transitions import Advance, advance_consultation, finish_find_themes
from tests.rows import make_consultation, make_department, make_open_question, make_queued_job

pytestmark = pytest.mark.db


def consultation_row(db: psycopg.Connection[DictRow], consultation_id: object) -> DictRow:
    row = db.execute(
        """
        SELECT status, run_id, awaiting_review_at IS NOT NULL AS awaiting_stamped,
               status_changed_at
          FROM consultation WHERE id = %s
        """,
        (consultation_id,),
    ).fetchone()
    assert row is not None
    return row


def outbox_rows(
    db: psycopg.Connection[DictRow], consultation_id: object
) -> list[dict[str, object]]:
    return db.execute(
        "SELECT kind, subject_id, status FROM notification_outbox WHERE consultation_id = %s ORDER BY id",
        (consultation_id,),
    ).fetchall()


def test_fan_in_one_flips_the_consultation_and_writes_one_outbox_row(
    db: psycopg.Connection[DictRow],
) -> None:
    consultation_id = make_consultation(db, make_department(db), status="processing")
    first = make_open_question(db, consultation_id, "o_reason")
    second = make_open_question(db, consultation_id, "o_safety", ordinal=2)
    before = consultation_row(db, consultation_id)

    first_lease = claim(db, make_queued_job(db, consultation_id, first), "worker-1")
    assert first_lease is not None
    assert finish_find_themes(db, first_lease, first, consultation_id) == Advance(False, False)
    assert consultation_row(db, consultation_id)["status"] == "processing"
    assert outbox_rows(db, consultation_id) == []

    second_lease = claim(db, make_queued_job(db, consultation_id, second), "worker-2")
    assert second_lease is not None
    assert finish_find_themes(db, second_lease, second, consultation_id) == Advance(True, False)

    after = consultation_row(db, consultation_id)
    assert after["status"] == "awaiting_review"
    assert after["awaiting_stamped"] is True
    # now() is the transaction's start, and this test is one transaction,
    # so the stamp can only be shown not to go backwards here.
    assert after["status_changed_at"] >= before["status_changed_at"]
    # One email, for this pass: the row names the run id (docs/04, section 2).
    assert outbox_rows(db, consultation_id) == [
        {"kind": "themes_ready", "subject_id": after["run_id"], "status": "pending"}
    ]
    statuses = db.execute(
        "SELECT status FROM question WHERE consultation_id = %s ORDER BY ordinal",
        (consultation_id,),
    ).fetchall()
    assert [row["status"] for row in statuses] == ["themes_ready", "themes_ready"]
    jobs = db.execute(
        "SELECT status FROM job WHERE consultation_id = %s", (consultation_id,)
    ).fetchall()
    assert {row["status"] for row in jobs} == {"succeeded"}

    # The reconciler's fourth statement runs the same routine again and
    # finds nothing to do: no second flip, no second row.
    assert advance_consultation(db, consultation_id) == Advance(False, False)
    assert len(outbox_rows(db, consultation_id)) == 1
