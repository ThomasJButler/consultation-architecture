"""The two tests ADR-001 promises.

The first runs twenty finishers at once, each on its own connection, and
counts the flips: one. The second walks the lost update by hand on two
connections without the row lock, so the failure the design guards
against is on record beside the guard.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow

from consult import store
from consult.config import Settings
from consult.jobs import claim
from consult.transitions import Advance, advance_consultation, finish_find_themes
from tests.rows import make_consultation, make_department, make_open_question, make_queued_job

pytestmark = pytest.mark.db

FINISHERS = 20


@pytest.mark.slow
def test_the_fan_in_flips_exactly_once_under_twenty_threaded_finishers(
    db: psycopg.Connection[DictRow], db_settings: Settings
) -> None:
    consultation_id = make_consultation(db, make_department(db), status="processing")
    work: list[tuple[UUID, UUID]] = []
    for ordinal in range(1, FINISHERS + 1):
        question_id = make_open_question(db, consultation_id, f"o_{ordinal}", ordinal=ordinal)
        work.append((question_id, make_queued_job(db, consultation_id, question_id)))
    # Committed, so the other connections can see the rows.
    db.commit()

    barrier = threading.Barrier(FINISHERS)

    def finish(index: int, question_id: UUID, job_id: UUID) -> Advance:
        with store.connect(db_settings) as conn:
            lease = claim(conn, job_id, f"worker-{index}")
            assert lease is not None
            # Everyone claims, then everyone finishes at once.
            barrier.wait()
            advance = finish_find_themes(conn, lease, question_id, consultation_id)
            conn.commit()
            return advance

    with ThreadPoolExecutor(max_workers=FINISHERS) as pool:
        advances = list(pool.map(lambda item: finish(item[0], *item[1]), enumerate(work)))

    assert sum(advance.themes_ready for advance in advances) == 1
    row = db.execute("SELECT status FROM consultation WHERE id = %s", (consultation_id,)).fetchone()
    assert row == {"status": "awaiting_review"}
    outbox = db.execute(
        "SELECT count(*) AS rows FROM notification_outbox WHERE consultation_id = %s",
        (consultation_id,),
    ).fetchone()
    assert outbox == {"rows": 1}
    finished = db.execute(
        "SELECT count(*) AS rows FROM question WHERE consultation_id = %s AND status = 'themes_ready'",
        (consultation_id,),
    ).fetchone()
    assert finished == {"rows": FINISHERS}


def test_without_the_row_lock_two_finishers_lose_the_update(
    db: psycopg.Connection[DictRow], db_settings: Settings
) -> None:
    consultation_id = make_consultation(db, make_department(db), status="processing")
    first = make_open_question(db, consultation_id, "o_first", ordinal=1)
    second = make_open_question(db, consultation_id, "o_second", ordinal=2)
    db.commit()

    # The guarded UPDATE from docs/02 step 7 with the lock left out.
    flip = """
        UPDATE consultation SET status = 'awaiting_review'
         WHERE id = %(id)s AND status = 'processing'
           AND NOT EXISTS (SELECT 1 FROM question
                            WHERE consultation_id = %(id)s AND kind = 'open'
                              AND status IN ('configured', 'finding_themes'))
    """
    with store.connect(db_settings) as one, store.connect(db_settings) as two:
        # Each finisher moves its own question inside its own transaction.
        one.execute("UPDATE question SET status = 'themes_ready' WHERE id = %s", (first,))
        two.execute("UPDATE question SET status = 'themes_ready' WHERE id = %s", (second,))
        # Under READ COMMITTED each statement's snapshot predates the other
        # transaction's commit, so each sees the other's question still
        # running, matches no row, and takes no lock (PostgreSQL 17 manual,
        # 13.2.1, via docs/01).
        assert one.execute(flip, {"id": consultation_id}).rowcount == 0
        assert two.execute(flip, {"id": consultation_id}).rowcount == 0
        one.commit()
        two.commit()

    row = db.execute("SELECT status FROM consultation WHERE id = %s", (consultation_id,)).fetchone()
    stuck = db.execute(
        "SELECT count(*) AS rows FROM question WHERE consultation_id = %s AND status = 'themes_ready'",
        (consultation_id,),
    ).fetchone()
    # Both questions are done and the consultation sits in processing for
    # ever: the lost update. That's why the lock comes first, and why the
    # reconciler's fourth statement exists.
    assert (row, stuck) == ({"status": "processing"}, {"rows": 2})
    assert advance_consultation(db, consultation_id) == Advance(True, False)
