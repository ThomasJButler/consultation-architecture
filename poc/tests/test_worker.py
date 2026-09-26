"""What the worker's model boundary promises on a gateway failure (docs/02,
section 9; ADR-005's "one to sixty seconds, six attempts").

Driven on a queued and claimed map_themes job, with the fake scripted to
raise `llm.GatewayError` on every attempt: the retry count, the full-jitter
waits, that no transaction is open for a call or a sleep, and that the
failure that lands on the job row and the log line is a code and a request
id, never the provider's text.
"""

from __future__ import annotations

import io
import logging
import random

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from psycopg.rows import DictRow

from consult.errors import ErrorCode
from consult.jobs import claim
from consult.llm import LLM, Completion, GatewayError, Prompt
from consult.logs import Formatter
from consult.mapping import run_map_themes
from consult.worker import (
    BACKOFF_ATTEMPTS,
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    BackingOff,
    record_gateway_failure,
)
from tests.fakes import FakeLLM
from tests.pipeline import signed_off_fixture

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
