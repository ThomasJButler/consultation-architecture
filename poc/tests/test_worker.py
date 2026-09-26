"""What the worker promises: its model boundary on a gateway failure, and
the loop that picks, claims and runs a job.

The first test drives a queued and claimed map_themes job with the fake
scripted to raise `llm.GatewayError` on every attempt (docs/02, section 9;
ADR-005's "one to sixty seconds, six attempts"): the retry count, the
full-jitter waits, that no transaction is open for a call or a sleep, and
that the failure that lands on the job row and the log line is a code and
a request id, never the provider's text.

The second drives `worker.run_once` on the fixtures (docs/02, step 5): the
oldest runnable job first, each kind through its own runner, a stale lease
taken over, a spent retry budget left alone, and workers racing on their
own connections never sharing a job.
"""

from __future__ import annotations

import io
import logging
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from psycopg.rows import DictRow

from consult import store
from consult.config import Settings
from consult.dispatch import dispatch
from consult.errors import ErrorCode
from consult.jobs import claim
from consult.llm import LLM, Completion, GatewayError, Prompt
from consult.logs import Formatter
from consult.mapping import run_map_themes
from consult.transitions import sign_off
from consult.worker import (
    BACKOFF_ATTEMPTS,
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    BackingOff,
    Outcome,
    record_gateway_failure,
    run_once,
)
from tests.fakes import FakeLLM, RecordingLLM
from tests.pipeline import dispatched_fixture, signed_off_fixture

pytestmark = pytest.mark.db

# Distinctive enough that it can't appear by accident anywhere else this
# test looks (a job row, a job_batch row, the rendered log line).
GATEWAY_MESSAGE = "upstream said: The towpath floods"


class _TransactionCheckingLLM:
    """Wraps a fake so every call records the connection's transaction
    status at the moment it's made (the pin that no write is pending while
    the gateway is being asked)."""

    def __init__(self, conn: psycopg.Connection[DictRow], llm: LLM) -> None:
        self.conn = conn
        self.llm = llm
        self.statuses: list[TransactionStatus] = []

    def complete(self, prompt: Prompt) -> Completion:
        self.statuses.append(self.conn.info.transaction_status)
        return self.llm.complete(prompt)


class _RecordingSleeper:
    """Records the seconds it was asked to wait, and the connection's
    transaction status at each ask (the same pin, for the backoff sleep)."""

    def __init__(self, conn: psycopg.Connection[DictRow]) -> None:
        self.conn = conn
        self.waits: list[float] = []
        self.statuses: list[TransactionStatus] = []

    def __call__(self, seconds: float) -> None:
        self.statuses.append(self.conn.info.transaction_status)
        self.waits.append(seconds)


class _UpperBoundRandom(random.Random):
    """A stub draw that always returns the upper bound, so each wait comes
    out exactly at AWS's full-jitter ceiling: min(cap, base * 2**attempt)
    (docs/02, section 9, cited in `worker.backoff_seconds`)."""

    def uniform(self, a: float, b: float) -> float:
        return b


def test_a_gateway_error_backs_off_then_records_a_code(db: psycopg.Connection[DictRow]) -> None:
    signed = signed_off_fixture(db)
    lease = claim(db, signed.job_id, "worker-1")
    assert lease is not None

    fault = GatewayError(
        ErrorCode.GATEWAY_UNAVAILABLE, request_id="req_abc123", message=GATEWAY_MESSAGE
    )
    fake = FakeLLM([fault] * BACKOFF_ATTEMPTS)
    checked = _TransactionCheckingLLM(db, fake)
    sleeper = _RecordingSleeper(db)
    rng = _UpperBoundRandom()
    backing_off = BackingOff(checked, sleep=sleeper, rng=rng, before_call=db.commit)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(Formatter())
    root = logging.getLogger()
    root.addHandler(handler)
    previous_level = root.level
    root.setLevel(logging.INFO)
    try:
        with pytest.raises(GatewayError) as excinfo:
            run_map_themes(db, backing_off, lease)
        db.rollback()
        record_gateway_failure(db, lease, excinfo.value)
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    # Six attempts, five sleeps: no sleep after the last (docs/02, section 9).
    assert len(fake.prompts) == BACKOFF_ATTEMPTS
    assert not fake.script
    assert len(sleeper.waits) == BACKOFF_ATTEMPTS - 1

    # Full jitter: random.uniform(0, min(cap, base * 2**attempt)), AWS's
    # formula for the n-th retry. The stub draws the upper bound each time,
    # so the waits come out exact, and they're inside the design's 1 to 60 s
    # range either way.
    expected_waits = [
        float(min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2**attempt))
        for attempt in range(BACKOFF_ATTEMPTS - 1)
    ]
    assert sleeper.waits == expected_waits
    assert all(1.0 <= wait <= 60.0 for wait in sleeper.waits)

    # No transaction open for any call or any sleep: the worker's
    # `before_call` (here `db.commit`) runs ahead of every attempt, so a
    # heartbeat stamped after a wait is never already stale (`now()` is
    # transaction start, PostgreSQL 17 manual 9.9.5; docs/07 row 05).
    assert checked.statuses == [TransactionStatus.IDLE] * BACKOFF_ATTEMPTS
    assert sleeper.statuses == [TransactionStatus.IDLE] * (BACKOFF_ATTEMPTS - 1)

    row = db.execute(
        "SELECT status, error_code, provider_request_id, next_attempt_at > now() AS later"
        " FROM job WHERE id = %s",
        (signed.job_id,),
    ).fetchone()
    assert row == {
        "status": "failed_retryable",
        "error_code": "gateway_unavailable",
        "provider_request_id": "req_abc123",
        "later": True,
    }

    # The message never reaches a row: not job's, not job_batch's (there
    # isn't one, since the very first batch never got a checkpoint).
    job_row = db.execute("SELECT * FROM job WHERE id = %s", (signed.job_id,)).fetchone()
    assert job_row is not None
    assert GATEWAY_MESSAGE not in " ".join(str(value) for value in job_row.values())
    batches = db.execute("SELECT * FROM job_batch WHERE job_id = %s", (signed.job_id,)).fetchall()
    assert batches == []

    # Nor the log line: the fields an allow-listed event carries, and
    # nothing the provider said (THREAT_MODEL.md, section 2; CLAUDE.md,
    # rule 8).
    output = stream.getvalue()
    assert GATEWAY_MESSAGE not in output
    assert "towpath" not in output
    assert "job_failed" in output
    assert f"job_id={lease.job_id}" in output
    assert "error_code=gateway_unavailable" in output
    assert "provider_request_id=req_abc123" in output


def test_backoff_does_not_retry_a_rejected_request(db: psycopg.Connection[DictRow]) -> None:
    # GATEWAY_REJECTED is a 4xx, the request's own fault (errors.py), and
    # docs/02 section 9 and ADR-005 back off only on a 429 or a 5xx. One
    # call, no sleep, then the code and request id land on the row exactly
    # as a retried failure would.
    signed = signed_off_fixture(db)
    lease = claim(db, signed.job_id, "worker-1")
    assert lease is not None

    fault = GatewayError(ErrorCode.GATEWAY_REJECTED, request_id="req_rejected")
    fake = FakeLLM([fault])
    sleeper = _RecordingSleeper(db)
    backing_off = BackingOff(fake, sleep=sleeper, before_call=db.commit)

    with pytest.raises(GatewayError) as excinfo:
        run_map_themes(db, backing_off, lease)
    db.rollback()
    record_gateway_failure(db, lease, excinfo.value)

    assert len(fake.prompts) == 1
    assert not fake.script
    assert sleeper.waits == []

    row = db.execute(
        "SELECT status, error_code, provider_request_id FROM job WHERE id = %s",
        (signed.job_id,),
    ).fetchone()
    assert row == {
        "status": "failed_retryable",
        "error_code": "gateway_rejected",
        "provider_request_id": "req_rejected",
    }


# ADR-002: "the retry budget is job.attempts < 5".
RETRY_BUDGET = 5
# docs/02, step 5: ten minutes of silence and a lease can be taken over.
# A minute either side of that is a lease gone stale and one that hasn't.
STALE = timedelta(minutes=11)
LIVE = timedelta(minutes=9)
# More workers than the race has jobs, so the ones left over show too.
RACERS = 6


def _by_id(db: psycopg.Connection[DictRow], consultation_id: UUID, kind: str) -> list[DictRow]:
    return db.execute(
        """
        SELECT id, question_id, created_at FROM job
         WHERE consultation_id = %s AND kind = %s ORDER BY id
        """,
        (consultation_id, kind),
    ).fetchall()


def _job(db: psycopg.Connection[DictRow], job_id: UUID) -> DictRow | None:
    return db.execute("SELECT * FROM job WHERE id = %s", (job_id,)).fetchone()


def _stages(db: psycopg.Connection[DictRow], job_id: UUID) -> set[str]:
    rows = db.execute("SELECT DISTINCT stage FROM job_batch WHERE job_id = %s", (job_id,))
    return {str(row["stage"]) for row in rows.fetchall()}


def _questions(db: psycopg.Connection[DictRow], consultation_id: UUID) -> dict[UUID, str]:
    rows = db.execute(
        "SELECT id, status FROM question WHERE consultation_id = %s AND kind = 'open'",
        (consultation_id,),
    ).fetchall()
    return {row["id"]: str(row["status"]) for row in rows}


def _consultation(db: psycopg.Connection[DictRow], consultation_id: UUID) -> DictRow | None:
    return db.execute(
        """
        SELECT c.status,
               (SELECT count(*) FROM notification_outbox o
                 WHERE o.consultation_id = c.id AND o.kind = 'analysis_ready') AS analysis_ready
          FROM consultation c WHERE c.id = %s
        """,
        (consultation_id,),
    ).fetchone()


def test_the_worker_claims_the_oldest_job_and_runs_it_by_kind(
    db: psycopg.Connection[DictRow], db_settings: Settings
) -> None:
    # One ingest gives both find_themes jobs its now(), so the one whose id
    # sorts second is moved a minute older: the pick goes by created_at
    # first and the id only breaks a tie (docs/02, step 5; plan section 2,
    # "Pick").
    consultation_id = dispatched_fixture(db, db_settings)
    lower, higher = _by_id(db, consultation_id, "find_themes")
    assert lower["created_at"] == higher["created_at"]
    db.execute(
        "UPDATE job SET created_at = created_at - %s WHERE id = %s",
        (timedelta(minutes=1), higher["id"]),
    )
    db.commit()
    llm = _TransactionCheckingLLM(db, RecordingLLM())
    sleeper = _RecordingSleeper(db)

    older = run_once(db, llm, worker="w1", sleep=sleeper)

    assert older == Outcome(higher["id"], "find_themes", "succeeded", 1)
    assert _questions(db, consultation_id) == {
        higher["question_id"]: "themes_ready",
        lower["question_id"]: "configured",
    }

    newer = run_once(db, llm, worker="w1", sleep=sleeper)

    assert newer == Outcome(lower["id"], "find_themes", "succeeded", 1)
    assert _consultation(db, consultation_id) == {"status": "awaiting_review", "analysis_ready": 0}
    # By kind: a find_themes job checkpoints generation, condensation and
    # preview (docs/02, step 6).
    assert (
        _stages(db, lower["id"])
        == _stages(db, higher["id"])
        == {
            "generate",
            "condense",
            "preview",
        }
    )

    # Both signed off in one transaction, so their map_themes jobs share a
    # created_at and the lower id goes first.
    for job in (lower, higher):
        assert sign_off(db, job["question_id"], uuid4()) is not None
    dispatch(db, db_settings)
    db.commit()
    first_map, second_map = _by_id(db, consultation_id, "map_themes")
    assert first_map["created_at"] == second_map["created_at"]

    mapped = [run_once(db, llm, worker="w1", sleep=sleeper) for _ in range(2)]

    assert mapped == [
        Outcome(first_map["id"], "map_themes", "succeeded", 1),
        Outcome(second_map["id"], "map_themes", "succeeded", 1),
    ]
    # A map_themes job checkpoints nothing but map batches (docs/02, step 9).
    assert _stages(db, first_map["id"]) == _stages(db, second_map["id"]) == {"map"}
    assert set(_questions(db, consultation_id).values()) == {"complete"}
    assert _consultation(db, consultation_id) == {"status": "ready", "analysis_ready": 1}
    assert run_once(db, llm, worker="w1", sleep=sleeper) is None
    # No transaction open at any call, and nothing backed off: the wiring
    # test_a_gateway_error_backs_off_then_records_a_code pins on BackingOff,
    # here through run_once (docs/02, section 9).
    assert llm.statuses
    assert set(llm.statuses) == {TransactionStatus.IDLE}
    assert sleeper.waits == []

    # A stale lease is taken over and a live one left alone. The live one
    # sorts first, so a pick that ignored the heartbeat would take it.
    taken_over = dispatched_fixture(db, db_settings)
    live, stale = _by_id(db, taken_over, "find_themes")
    for job, worker, silence in ((live, "w-alive", LIVE), (stale, "w-dead", STALE)):
        assert claim(db, job["id"], worker) is not None
        db.execute("UPDATE job SET heartbeat_at = now() - %s WHERE id = %s", (silence, job["id"]))
    db.commit()
    live_before = _job(db, live["id"])

    takeover = run_once(db, RecordingLLM(), worker="w2", sleep=sleeper)

    assert takeover == Outcome(stale["id"], "find_themes", "succeeded", 2)
    after = _job(db, stale["id"])
    assert after is not None
    assert (after["status"], after["attempts"], after["claimed_by"]) == ("succeeded", 2, "w2")
    assert _job(db, live["id"]) == live_before
    assert run_once(db, RecordingLLM(), worker="w2", sleep=sleeper) is None

    # A spent retry budget is left alone whatever the job's status: two
    # queued jobs at five attempts, and the live lease above gone silent on
    # its fifth. Failing them is the reconciler's call (docs/02, section 5).
    spent = dispatched_fixture(db, db_settings)
    db.execute("UPDATE job SET attempts = %s WHERE consultation_id = %s", (RETRY_BUDGET, spent))
    db.execute(
        "UPDATE job SET attempts = %s, heartbeat_at = now() - %s WHERE id = %s",
        (RETRY_BUDGET, STALE, live["id"]),
    )
    db.commit()
    before = db.execute("SELECT * FROM job ORDER BY id").fetchall()

    assert run_once(db, RecordingLLM(), worker="w3", sleep=sleeper) is None
    assert db.execute("SELECT * FROM job ORDER BY id").fetchall() == before

    # Six workers on six connections race for four queued jobs from two
    # ingests. The row lock the pick skips past and the conditional claim
    # give each job to exactly one worker, and the two left over get None
    # (docs/02, step 5; the pattern in test_fan_in_race.py).
    raced = [dispatched_fixture(db, db_settings) for _ in range(2)]
    queued = sorted(job["id"] for c in raced for job in _by_id(db, c, "find_themes"))
    assert len(queued) == 4
    db.commit()
    # With a timeout, a thread that fails before the barrier breaks it for
    # the rest and the test fails loudly instead of joining for ever.
    barrier = threading.Barrier(RACERS, timeout=30)
    waits: list[float] = []

    def race(index: int) -> Outcome | None:
        with store.connect(db_settings) as conn:
            barrier.wait()
            return run_once(conn, RecordingLLM(), worker=f"w-race-{index}", sleep=waits.append)

    with ThreadPoolExecutor(max_workers=RACERS) as pool:
        futures = [pool.submit(race, index) for index in range(RACERS)]
    failures = [f.exception() for f in futures if f.exception() is not None]
    assert failures == []
    outcomes = [f.result() for f in futures]

    assert sorted(o.job_id for o in outcomes if o is not None) == queued
    assert outcomes.count(None) == RACERS - len(queued)
    rows = db.execute(
        "SELECT status, attempts, claimed_by FROM job WHERE id = ANY(%s)", (queued,)
    ).fetchall()
    assert [(row["status"], row["attempts"]) for row in rows] == [("succeeded", 1)] * len(queued)
    assert len({row["claimed_by"] for row in rows}) == len(queued)
    assert all(
        _consultation(db, c) == {"status": "awaiting_review", "analysis_ready": 0} for c in raced
    )
    assert waits == []
