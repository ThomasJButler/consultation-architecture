"""What the store promises about a failed job.

THREAT_MODEL.md section 2, lines 1 to 3: a failed job stores an error code
from a fixed vocabulary and the provider's request id, there is no column a
message body could go in, and the log line that reports it carries ids,
counts, durations and codes only. docs/06 section 2.5 and CLAUDE.md rule 8
say the same; this file is what holds the code to it.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.errors import CheckViolation
from psycopg.rows import DictRow

from consult.errors import ErrorCode
from consult.logs import Formatter
from consult.store import record_failure
from tests.rows import make_consultation, make_department, make_running_job
from tests.test_logs import render

pytestmark = pytest.mark.db

# The text columns `job` is allowed: two enumerations, a worker name, a
# model alias, and the two a failure stores. Nothing a sentence fits in.
JOB_TEXT_COLUMNS = {
    "kind",
    "status",
    "claimed_by",
    "model_alias",
    "error_code",
    "provider_request_id",
}


def text_columns_of(conn: psycopg.Connection[DictRow], table: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT column_name
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = %s
           AND data_type IN ('text', 'character varying', 'character')
        """,
        (table,),
    ).fetchall()
    return {row["column_name"] for row in rows}


def test_job_error_and_log_lines_carry_ids_and_codes_only(
    db: psycopg.Connection[DictRow],
) -> None:
    assert text_columns_of(db, "job") == JOB_TEXT_COLUMNS

    consultation_id = make_consultation(db, make_department(db))
    job_id = make_running_job(db, consultation_id, claimed_by="worker-1", attempts=1)

    written = record_failure(
        db,
        job_id,
        claimed_by="worker-1",
        fence=1,
        error_code=ErrorCode.GATEWAY_TIMEOUT,
        provider_request_id="req_01J8ZW4",
        retry_in=timedelta(minutes=5),
    )

    assert written
    row = db.execute(
        """
        SELECT status, error_code, provider_request_id, attempts,
               next_attempt_at > now() AS retries_later
          FROM job WHERE id = %s
        """,
        (job_id,),
    ).fetchone()
    assert row == {
        "status": "failed_retryable",
        "error_code": "gateway_timeout",
        "provider_request_id": "req_01J8ZW4",
        "attempts": 1,
        "retries_later": True,
    }

    # The vocabulary is a constraint, not a convention: a message body in
    # the code column is refused by the database.
    with pytest.raises(CheckViolation):
        db.execute(
            "UPDATE job SET error_code = %s WHERE id = %s",
            ("502 Bad Gateway while sending 'Why do you feel that way?'", job_id),
        )
    db.rollback()

    line = render(
        Formatter(),
        "job_failed",
        job_id=job_id,
        consultation_id=consultation_id,
        attempts=1,
        answer_count=3,
        duration_ms=12.5,
        error_code=ErrorCode.GATEWAY_TIMEOUT,
        provider_request_id="req_01J8ZW4",
        answer="The towpath is underwater every winter.",
        prompt="You are tagging responses.",
        label="Flooding on the towpath",
        message="502 Bad Gateway",
        completion='{"assignments": []}',
        value_text="anything at all",
    )
    for kept in (
        f"job_id={job_id}",
        f"consultation_id={consultation_id}",
        "attempts=1",
        "answer_count=3",
        "duration_ms=12.5",
        "error_code=gateway_timeout",
        "provider_request_id=req_01J8ZW4",
    ):
        assert kept in line
    for dropped in ("towpath", "tagging", "Flooding", "Bad Gateway", "assignments", "anything"):
        assert dropped not in line


def test_a_stale_fence_records_nothing(db: psycopg.Connection[DictRow]) -> None:
    consultation_id = make_consultation(db, make_department(db))
    job_id = make_running_job(db, consultation_id, claimed_by="worker-1", attempts=2)

    # A worker whose lease was taken over holds fence 1; the job is on 2.
    written = record_failure(
        db, job_id, claimed_by="worker-1", fence=1, error_code=ErrorCode.LEASE_LOST
    )

    assert not written
    row = db.execute("SELECT status, error_code FROM job WHERE id = %s", (job_id,)).fetchone()
    assert row == {"status": "running", "error_code": None}


def test_every_error_code_passes_the_schema_check(db: psycopg.Connection[DictRow]) -> None:
    consultation_id = make_consultation(db, make_department(db))
    for code in ErrorCode:
        job_id = make_running_job(db, consultation_id, kind="preview_themes")
        assert record_failure(db, job_id, claimed_by="worker-1", fence=1, error_code=code)


def test_an_unknown_job_records_nothing(db: psycopg.Connection[DictRow]) -> None:
    assert not record_failure(
        db, uuid4(), claimed_by="worker-1", fence=1, error_code=ErrorCode.WORKER_ERROR
    )
