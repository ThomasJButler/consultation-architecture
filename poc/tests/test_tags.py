"""What the tag insert promises (docs/02, step 9; ADR-004).

Tags go in with ON CONFLICT DO NOTHING on the full unique index, so a
batch replayed after a takeover inserts nothing new; a person retracts a
tag in place rather than deleting it, and because the index is full and
not partial on live rows, the replay can't resurrect what was retracted.
"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import DictRow

from consult.jobs import LeaseLostError, claim
from consult.tags import Tag, add_human_tag, insert_tags, restore, retract
from tests.rows import (
    make_answer,
    make_consultation,
    make_department,
    make_open_question,
    make_queued_job,
    make_respondent,
    make_theme,
    make_theme_set_version,
)

pytestmark = pytest.mark.db


def test_tag_inserts_are_idempotent_and_a_retracted_tag_stays_retracted(
    db: psycopg.Connection[DictRow],
) -> None:
    consultation_id = make_consultation(db, make_department(db), status="awaiting_review")
    question_id = make_open_question(db, consultation_id, status="assigning_themes")
    version_id = make_theme_set_version(db, question_id, version_no=2, status="signed_off")
    parking = make_theme(db, version_id, "PARKING")
    safety = make_theme(db, version_id, "SAFETY")
    respondent = make_respondent(db, consultation_id, 2)
    first = make_answer(db, consultation_id, respondent, question_id, "We'd lose the parking.")
    second = make_answer(
        db, consultation_id, make_respondent(db, consultation_id, 3), question_id, "Unsafe."
    )
    lease = claim(
        db, make_queued_job(db, consultation_id, question_id, kind="map_themes"), "worker-1"
    )
    assert lease is not None
    batch = [Tag(first, parking), Tag(first, safety), Tag(second, safety)]

    assert insert_tags(db, lease, version_id, batch_no=1, tags=batch) == 3
    # The same batch again, as a worker taking over would send it.
    assert insert_tags(db, lease, version_id, batch_no=1, tags=batch) == 0

    tag = db.execute(
        "SELECT id, source, job_id, batch_no FROM answer_theme WHERE answer_id = %s AND theme_id = %s",
        (first, parking),
    ).fetchone()
    assert tag is not None
    assert (tag["source"], tag["job_id"], tag["batch_no"]) == ("ai", lease.job_id, 1)

    reviewer = uuid4()
    assert retract(db, tag["id"], reviewer) is True
    assert retract(db, tag["id"], reviewer) is False
    # The replay lands on the retracted row and does nothing (ADR-004).
    assert insert_tags(db, lease, version_id, batch_no=1, tags=batch) == 0
    row = db.execute(
        "SELECT retracted_at IS NOT NULL AS retracted, retracted_by FROM answer_theme WHERE id = %s",
        (tag["id"],),
    ).fetchone()
    assert row == {"retracted": True, "retracted_by": reviewer}

    assert restore(db, tag["id"]) is True
    assert restore(db, tag["id"]) is False
    live = db.execute(
        "SELECT count(*) AS n FROM answer_theme WHERE theme_set_version_id = %s AND retracted_at IS NULL",
        (version_id,),
    ).fetchone()
    assert live == {"n": 3}

    # A person adding a tag the model didn't: a human row, and re-adding a
    # retracted one clears the retraction rather than duplicating the row.
    human = add_human_tag(db, second, parking, version_id, reviewer)
    assert retract(db, human, reviewer) is True
    assert add_human_tag(db, second, parking, version_id, reviewer) == human
    sources = db.execute(
        "SELECT source, count(*) AS n FROM answer_theme WHERE theme_set_version_id = %s "
        "AND retracted_at IS NULL GROUP BY source ORDER BY source",
        (version_id,),
    ).fetchall()
    assert [(s["source"], s["n"]) for s in sources] == [("ai", 3), ("human", 1)]

    # And the insert is fenced like every other worker write.
    db.execute(
        "UPDATE job SET heartbeat_at = now() - interval '11 minutes' WHERE id = %s", (lease.job_id,)
    )
    assert claim(db, lease.job_id, "worker-2") is not None
    with pytest.raises(LeaseLostError):
        insert_tags(db, lease, version_id, batch_no=2, tags=[Tag(second, parking)])
