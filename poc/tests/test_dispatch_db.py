"""What `dispatch.dispatch` promises against Postgres (docs/02, step 4).

A module of its own, apart from `tests/test_dispatch.py`, because
test_repo_rules.py marks whole modules: this one needs a database and is
marked db, and the pure pick has to stay runnable under
`pytest -m 'not db'`.

Every expected count is a cap the test sets or a hand count of the rows
it made, never read off the code under test.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow

from consult import store
from consult.config import Settings
from consult.dispatch import dispatch
from tests.rows import make_consultation, make_department, make_open_question, make_pending_job

pytestmark = pytest.mark.db


def _statuses(db: psycopg.Connection[DictRow], consultation_id: UUID) -> dict[str, int]:
    rows = db.execute(
        "SELECT status, count(*) AS n FROM job WHERE consultation_id = %s GROUP BY status",
        (consultation_id,),
    ).fetchall()
    return {str(row["status"]): int(row["n"]) for row in rows}


def _questions(db: psycopg.Connection[DictRow], consultation_id: UUID, count: int) -> list[UUID]:
    # One open question per job: job_one_per_run allows one find_themes
    # job per question per run (schema.sql).
    return [
        make_open_question(db, consultation_id, f"o_{n}", ordinal=n, status="configured")
        for n in range(1, count + 1)
    ]


def test_dispatch_queues_under_the_caps_and_keeps_a_seed(
    db: psycopg.Connection[DictRow], db_settings: Settings
) -> None:
    settings = replace(db_settings, model_alias="fake-dispatch")

    # Three pending jobs in one consultation, under every cap (6, 4, 20):
    # a find_themes and a map_themes job as ingest and sign-off insert
    # them, and a find_themes job back at pending after a failure, still
    # carrying the alias and seed its first dispatch stamped.
    stamped_in = make_consultation(db, make_department(db), status="processing")
    first, second, third = _questions(db, stamped_in, 3)
    fresh_find = make_pending_job(db, stamped_in, first)
    fresh_map = make_pending_job(db, stamped_in, second, kind="map_themes")
    retried = make_pending_job(db, stamped_in, third, model_alias="fake-earlier", seed=424242)

    assert dispatch(db, settings) == 3
    stamped = {
        row["id"]: row
        for row in db.execute(
            "SELECT id, status, sent_at, model_alias, params FROM job WHERE consultation_id = %s",
            (stamped_in,),
        ).fetchall()
    }
    for job_id in (fresh_find, fresh_map):
        row = stamped[job_id]
        assert (row["status"], row["model_alias"]) == ("queued", "fake-dispatch")
        assert row["sent_at"] is not None
        assert isinstance(row["params"].get("seed"), int)
    # The retried job's checkpoints were cut from the plan its first seed
    # shuffled, so the seed and the alias stay and the plan can be rebuilt
    # (ADR-002).
    kept = stamped[retried]
    assert (kept["status"], kept["model_alias"], kept["params"]) == (
        "queued",
        "fake-earlier",
        {"seed": 424242},
    )
    assert kept["sent_at"] is not None

    # The consultation cap comes from the settings: two here, so three
    # pending jobs overfill it. Two go, a second run queues nothing, and
    # a slot freed by hand lets exactly one more go.
    capped = replace(settings, jobs_per_consultation=2)
    crowded = make_consultation(db, make_department(db), status="processing")
    for question_id in _questions(db, crowded, 3):
        make_pending_job(db, crowded, question_id)
    assert dispatch(db, capped) == 2
    assert dispatch(db, capped) == 0
    assert _statuses(db, crowded) == {"queued": 2, "pending": 1}
    db.execute(
        """
        UPDATE job SET status = 'succeeded'
         WHERE id = (SELECT id FROM job WHERE consultation_id = %s AND status = 'queued' LIMIT 1)
        """,
        (crowded,),
    )
    assert dispatch(db, capped) == 1
    assert _statuses(db, crowded) == {"succeeded": 1, "queued": 2}

    # Two inserting transactions at once in one department whose cap is
    # three, each with three jobs of its own not yet committed. Each sees
    # only its own jobs, so without a lock each would see three free
    # slots and take them: six queued. The advisory lock makes the second
    # wait for the first's commit and then find the slots gone.
    department = make_department(db)
    db.execute("UPDATE department SET concurrent_jobs_cap = 3 WHERE id = %s", (department,))
    work: list[tuple[UUID, list[UUID]]] = []
    for _ in range(2):
        consultation_id = make_consultation(db, department, status="processing")
        work.append((consultation_id, _questions(db, consultation_id, 3)))
    # Committed, so the other connections can see the rows, and the
    # advisory lock the dispatches above took on this connection is let go.
    db.commit()

    # With a timeout, a thread that fails before the barrier breaks it for
    # the other and the test fails loudly instead of the pool joining for ever.
    barrier = threading.Barrier(len(work), timeout=30)

    def insert_and_dispatch(consultation_id: UUID, question_ids: list[UUID]) -> int:
        with store.connect(db_settings) as conn:
            for question_id in question_ids:
                make_pending_job(conn, consultation_id, question_id)
            barrier.wait()
            queued = dispatch(conn, settings)
            conn.commit()
            return queued

    with ThreadPoolExecutor(max_workers=len(work)) as pool:
        futures = [pool.submit(insert_and_dispatch, *pair) for pair in work]
    # Every failure, not just the first future's, so the root cause shows
    # rather than the BrokenBarrierError the other raises after it.
    failures = [f.exception() for f in futures if f.exception() is not None]
    assert failures == []
    assert sum(f.result() for f in futures) == 3
    counted = db.execute(
        """
        SELECT count(*) FILTER (WHERE status = 'queued') AS queued,
               count(*) FILTER (WHERE status = 'pending') AS pending
          FROM job WHERE department_id = %s
        """,
        (department,),
    ).fetchone()
    assert counted == {"queued": 3, "pending": 3}
