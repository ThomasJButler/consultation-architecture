"""What the fan-ins promise (docs/02, steps 7 and 10; ADR-001; ADR-006).

A worker finishing a question locks the consultation row, moves the
question on, checks whether every open question has reached the
milestone, flips the consultation if so and writes the email row, in one
commit. One routine, advance_consultation, is the only writer of the
consultation's status.
"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import DictRow

from consult.jobs import claim
from consult.transitions import (
    Advance,
    advance_consultation,
    finish_find_themes,
    finish_map_themes,
    reopen_for_correction,
    sign_off,
)
from tests.rows import (
    make_consultation,
    make_department,
    make_open_question,
    make_queued_job,
    make_theme,
    make_theme_set_version,
)

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


def set_status(db: psycopg.Connection[DictRow], question_id: object, status: str) -> None:
    db.execute("UPDATE question SET status = %s WHERE id = %s", (status, question_id))


def test_fan_in_one_waits_for_configured_and_finding_and_not_for_failed_or_signed_off(
    db: psycopg.Connection[DictRow],
) -> None:
    consultation_id = make_consultation(db, make_department(db), status="processing")
    make_open_question(db, consultation_id, "o_quick", status="signed_off", ordinal=1)
    make_open_question(db, consultation_id, "o_failed", status="find_failed", ordinal=2)
    third = make_open_question(db, consultation_id, "o_third", ordinal=3)
    fourth = make_open_question(db, consultation_id, "o_fourth", status="configured", ordinal=4)

    # A sibling still configured holds the milestone back.
    lease = claim(db, make_queued_job(db, consultation_id, third), "worker-1")
    assert lease is not None
    assert finish_find_themes(db, lease, third, consultation_id) == Advance(False, False)

    # Once it has run, the failed sibling and the one a quick reviewer has
    # already signed off don't: four ready questions shouldn't wait on an
    # operator, and the signed-off one is past the milestone (docs/02, step 7).
    set_status(db, fourth, "finding_themes")
    lease = claim(db, make_queued_job(db, consultation_id, fourth), "worker-1")
    assert lease is not None
    assert finish_find_themes(db, lease, fourth, consultation_id) == Advance(True, False)
    assert consultation_row(db, consultation_id)["status"] == "awaiting_review"


def test_fan_in_two_needs_every_open_question_complete(db: psycopg.Connection[DictRow]) -> None:
    consultation_id = make_consultation(db, make_department(db), status="awaiting_review")
    make_open_question(db, consultation_id, "o_done", status="complete", ordinal=1)
    second = make_open_question(
        db, consultation_id, "o_second", status="assigning_themes", ordinal=2
    )
    third = make_open_question(db, consultation_id, "o_third", status="map_failed", ordinal=3)

    # A failed mapping blocks ready by design (docs/02, step 10).
    lease = claim(db, make_queued_job(db, consultation_id, second, kind="map_themes"), "worker-1")
    assert lease is not None
    assert finish_map_themes(db, lease, second, consultation_id) == Advance(False, False)
    assert consultation_row(db, consultation_id)["status"] == "awaiting_review"
    assert outbox_rows(db, consultation_id) == []

    set_status(db, third, "assigning_themes")
    lease = claim(db, make_queued_job(db, consultation_id, third, kind="map_themes"), "worker-2")
    assert lease is not None
    assert finish_map_themes(db, lease, third, consultation_id) == Advance(False, True)

    after = consultation_row(db, consultation_id)
    assert after["status"] == "ready"
    assert outbox_rows(db, consultation_id) == [
        {"kind": "analysis_ready", "subject_id": after["run_id"], "status": "pending"}
    ]


def test_sign_off_is_a_guarded_update_that_admits_one_reviewer(
    db: psycopg.Connection[DictRow],
) -> None:
    consultation_id = make_consultation(db, make_department(db), status="awaiting_review")
    question_id = make_open_question(db, consultation_id, status="themes_ready")
    candidate = make_theme_set_version(db, question_id)
    parking = make_theme(db, candidate, "PARKING", "Loss of parking on Mill Lane")
    make_theme(db, candidate, "SAFETY", "Junction safety at the bridge")
    reviewer, rival = uuid4(), uuid4()

    signed = sign_off(db, question_id, reviewer)

    assert signed is not None
    # The guard is the mutex: the second reviewer gets nothing (ADR-003).
    assert sign_off(db, question_id, rival) is None
    question = db.execute("SELECT status FROM question WHERE id = %s", (question_id,)).fetchone()
    assert question == {"status": "signed_off"}

    # v2 is frozen with stable keys plus the two fallbacks, v1 is superseded.
    versions = db.execute(
        """
        SELECT id, version_no, status, parent_version_id, signed_off_by
          FROM theme_set_version WHERE question_id = %s ORDER BY version_no
        """,
        (question_id,),
    ).fetchall()
    assert [
        (v["version_no"], v["status"], v["parent_version_id"], v["signed_off_by"]) for v in versions
    ] == [
        (1, "superseded", None, None),
        (2, "signed_off", candidate, reviewer),
    ]
    assert versions[1]["id"] == signed.version_id
    themes = db.execute(
        "SELECT key, is_fallback, lineage_theme_id FROM theme WHERE theme_set_version_id = %s ORDER BY key",
        (signed.version_id,),
    ).fetchall()
    assert [(t["key"], t["is_fallback"]) for t in themes] == [
        ("NO_REASON", True),
        ("OTHER", True),
        ("PARKING", False),
        ("SAFETY", False),
    ]
    assert next(t["lineage_theme_id"] for t in themes if t["key"] == "PARKING") == parking

    # One map job for this question only, on the consultation's pass.
    jobs_ = db.execute(
        """
        SELECT j.id, j.kind, j.status, j.run_id = c.run_id AS this_pass
          FROM job j JOIN consultation c ON c.id = j.consultation_id
         WHERE j.question_id = %s
        """,
        (question_id,),
    ).fetchall()
    assert [(j["kind"], j["status"], j["this_pass"]) for j in jobs_] == [
        ("map_themes", "pending", True)
    ]
    assert jobs_[0]["id"] == signed.job_id


def test_a_reopen_mints_a_run_id_so_the_second_email_has_its_own_row(
    db: psycopg.Connection[DictRow],
) -> None:
    consultation_id = make_consultation(db, make_department(db), status="awaiting_review")
    question_id = make_open_question(db, consultation_id, status="assigning_themes")
    make_theme_set_version(db, question_id, version_no=1, status="superseded")
    signed = make_theme_set_version(db, question_id, version_no=2, status="signed_off")
    parking = make_theme(db, signed, "PARKING")
    lease = claim(
        db, make_queued_job(db, consultation_id, question_id, kind="map_themes"), "worker-1"
    )
    assert lease is not None
    assert finish_map_themes(db, lease, question_id, consultation_id) == Advance(False, True)
    first_pass = consultation_row(db, consultation_id)["run_id"]
    reviewer = uuid4()

    reopened = reopen_for_correction(db, question_id, reviewer)

    after = consultation_row(db, consultation_id)
    assert after["status"] == "awaiting_review"
    assert after["run_id"] == reopened.run_id != first_pass
    question = db.execute("SELECT status FROM question WHERE id = %s", (question_id,)).fetchone()
    assert question == {"status": "themes_ready"}
    candidate = db.execute(
        "SELECT version_no, status, parent_version_id FROM theme_set_version WHERE id = %s",
        (reopened.candidate_version_id,),
    ).fetchone()
    assert candidate == {"version_no": 3, "status": "candidate", "parent_version_id": signed}
    lineage = db.execute(
        "SELECT key, lineage_theme_id FROM theme WHERE theme_set_version_id = %s",
        (reopened.candidate_version_id,),
    ).fetchall()
    assert [(t["key"], t["lineage_theme_id"]) for t in lineage] == [("PARKING", parking)]

    # The second pass runs like the first and earns its own email row,
    # because the row's subject is the pass (docs/04, section 2).
    second = sign_off(db, question_id, reviewer)
    assert second is not None
    # Dispatch (pending to queued) is the reconciler's, PR-08; done by hand here.
    db.execute("UPDATE job SET status = 'queued', sent_at = now() WHERE id = %s", (second.job_id,))
    lease = claim(db, second.job_id, "worker-2")
    assert lease is not None
    set_status(db, question_id, "assigning_themes")
    assert finish_map_themes(db, lease, question_id, consultation_id) == Advance(False, True)
    rows = outbox_rows(db, consultation_id)
    assert [(r["kind"], r["subject_id"]) for r in rows] == [
        ("analysis_ready", first_pass),
        ("analysis_ready", reopened.run_id),
    ]
