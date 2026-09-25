"""What the claim, the heartbeat and the checkpoint promise (docs/02,
step 5; ADR-002).

The claim is one conditional UPDATE and the number it returns is the
fence: every later write the worker makes carries it, and a write with a
stale fence does nothing. That's how a worker that was taken over can't
spend or corrupt after it wakes up.
"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import DictRow

from consult.jobs import Lease, claim
from tests.rows import make_consultation, make_department, make_open_question, make_queued_job

pytestmark = pytest.mark.db


def test_claim_returns_the_fence_and_refuses_a_live_lease(db: psycopg.Connection[DictRow]) -> None:
    consultation_id = make_consultation(db, make_department(db))
    question_id = make_open_question(db, consultation_id)
    job_id = make_queued_job(db, consultation_id, question_id)

    lease = claim(db, job_id, "worker-1")

    # The first claim's attempts is 1, and that number is the fence.
    assert lease == Lease(job_id, "worker-1", 1)
    row = db.execute(
        "SELECT status, attempts, claimed_by, heartbeat_at IS NOT NULL AS beating FROM job WHERE id = %s",
        (job_id,),
    ).fetchone()
    assert row == {"status": "running", "attempts": 1, "claimed_by": "worker-1", "beating": True}

    # A second worker meets a live lease and gets nothing; the first stands.
    assert claim(db, job_id, "worker-2") is None
    row = db.execute("SELECT claimed_by, attempts FROM job WHERE id = %s", (job_id,)).fetchone()
    assert row == {"claimed_by": "worker-1", "attempts": 1}

    # So does a job that doesn't exist, or one already finished.
    assert claim(db, uuid4(), "worker-2") is None
    done = make_queued_job(
        db, consultation_id, question_id, kind="preview_themes", status="succeeded"
    )
    assert claim(db, done, "worker-2") is None
