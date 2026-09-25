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

from consult.errors import ErrorCode
from consult.jobs import Lease, LeaseLostError, checkpoint, claim, heartbeat, record_failure
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


def test_a_stale_lease_can_be_taken_over_and_the_fence_moves_on(
    db: psycopg.Connection[DictRow],
) -> None:
    consultation_id = make_consultation(db, make_department(db))
    job_id = make_queued_job(db, consultation_id, make_open_question(db, consultation_id))
    assert claim(db, job_id, "worker-1") == Lease(job_id, "worker-1", 1)

    # Nine minutes of silence isn't stale; ten is the threshold (docs/02, step 5).
    db.execute(
        "UPDATE job SET heartbeat_at = now() - interval '9 minutes' WHERE id = %s", (job_id,)
    )
    assert claim(db, job_id, "worker-2") is None
    db.execute(
        "UPDATE job SET heartbeat_at = now() - interval '11 minutes' WHERE id = %s", (job_id,)
    )

    assert claim(db, job_id, "worker-2") == Lease(job_id, "worker-2", 2)
    row = db.execute(
        "SELECT claimed_by, attempts, status FROM job WHERE id = %s", (job_id,)
    ).fetchone()
    assert row == {"claimed_by": "worker-2", "attempts": 2, "status": "running"}


def test_a_zombie_with_a_stale_fence_writes_nothing(db: psycopg.Connection[DictRow]) -> None:
    consultation_id = make_consultation(db, make_department(db))
    job_id = make_queued_job(db, consultation_id, make_open_question(db, consultation_id))
    zombie = claim(db, job_id, "worker-1")
    assert zombie is not None
    db.execute(
        "UPDATE job SET heartbeat_at = now() - interval '11 minutes' WHERE id = %s", (job_id,)
    )
    successor = claim(db, job_id, "worker-2")
    assert successor == Lease(job_id, "worker-2", 2)

    # The old process wakes up. Every write it tries starts with the fence
    # and every one is refused (ADR-002's walk-through of the failure).
    with pytest.raises(LeaseLostError):
        heartbeat(db, zombie)
    with pytest.raises(LeaseLostError):
        checkpoint(db, zombie, batch_no=1, stage="generate", answer_ids=[1, 2])
    with pytest.raises(LeaseLostError):
        record_failure(db, zombie, ErrorCode.GATEWAY_TIMEOUT)

    row = db.execute(
        "SELECT status, attempts, claimed_by, error_code FROM job WHERE id = %s", (job_id,)
    ).fetchone()
    assert row == {"status": "running", "attempts": 2, "claimed_by": "worker-2", "error_code": None}
    batches = db.execute(
        "SELECT count(*) AS n FROM job_batch WHERE job_id = %s", (job_id,)
    ).fetchone()
    assert batches == {"n": 0}
    # And the successor carries on as if nothing happened.
    heartbeat(db, successor)
    assert checkpoint(db, successor, batch_no=1, stage="generate", answer_ids=[1, 2]) is True
