"""What the store promises about a failed job.

THREAT_MODEL.md section 2, lines 1 to 3: a failed job stores an error code
from a fixed vocabulary and the provider's request id, there is no column a
message body could go in, and the log line that reports it carries ids,
counts, durations and codes only. docs/06 section 2.5 and CLAUDE.md rule 8
say the same; this file is what holds the code to it.
"""

from __future__ import annotations

import re
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.errors import CheckViolation
from psycopg.rows import DictRow

from consult.errors import ErrorCode
from consult.jobs import Lease, LeaseLostError, record_failure
from consult.logs import Formatter
from tests.rows import make_consultation, make_department, make_running_job
from tests.test_logs import render

pytestmark = pytest.mark.db

# The columns of `job` that can hold a string at all: two enumerations, a
# worker name, a model alias, the two a failure stores, and `params`, the
# model call's parameters as a JSON object (docs/04). Nothing a sentence
# fits in, and a CHECK keeps `params` an object rather than a bare string.
JOB_TEXT_COLUMNS = {
    "kind",
    "status",
    "claimed_by",
    "model_alias",
    "error_code",
    "provider_request_id",
    "params",
}


def text_columns_of(conn: psycopg.Connection[DictRow], table: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT column_name
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = %s
           AND data_type IN ('text', 'character varying', 'character', 'json', 'jsonb')
        """,
        (table,),
    ).fetchall()
    return {row["column_name"] for row in rows}


def check_literals_of(conn: psycopg.Connection[DictRow], table: str, column: str) -> set[str]:
    """The quoted literals in the CHECK constraint on one column."""
    rows = conn.execute(
        """
        SELECT pg_get_constraintdef(oid) AS definition
          FROM pg_constraint
         WHERE conrelid = %s::regclass AND contype = 'c'
           AND pg_get_constraintdef(oid) LIKE %s
        """,
        (table, f"%({column} = ANY%"),
    ).fetchall()
    assert len(rows) == 1
    return set(re.findall(r"'([a-z_]+)'::text", rows[0]["definition"]))


def test_job_error_and_log_lines_carry_ids_and_codes_only(
    db: psycopg.Connection[DictRow],
) -> None:
    assert text_columns_of(db, "job") == JOB_TEXT_COLUMNS

    consultation_id = make_consultation(db, make_department(db))
    job_id = make_running_job(db, consultation_id, claimed_by="worker-1", attempts=1)

    record_failure(
        db,
        Lease(job_id, "worker-1", 1),
        ErrorCode.GATEWAY_TIMEOUT,
        provider_request_id="req_01J8ZW4",
        retry_in=timedelta(minutes=5),
    )

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
    with pytest.raises(LeaseLostError):
        record_failure(db, Lease(job_id, "worker-1", 1), ErrorCode.LEASE_LOST)

    row = db.execute("SELECT status, error_code FROM job WHERE id = %s", (job_id,)).fetchone()
    assert row == {"status": "running", "error_code": None}


def test_the_schema_check_and_the_enum_name_the_same_codes(
    db: psycopg.Connection[DictRow],
) -> None:
    # Both directions: every enum value passes the CHECK, and the CHECK
    # names nothing the enum doesn't, so neither list can drift.
    assert check_literals_of(db, "job", "error_code") == {code.value for code in ErrorCode}
    consultation_id = make_consultation(db, make_department(db))
    for code in ErrorCode:
        job_id = make_running_job(db, consultation_id, kind="preview_themes")
        record_failure(db, Lease(job_id, "worker-1", 1), code)


def test_job_params_must_be_an_object(db: psycopg.Connection[DictRow]) -> None:
    # jsonb would happily store a bare string, which is a message body by
    # another route; the CHECK holds the column to an object of parameters.
    consultation_id = make_consultation(db, make_department(db))
    job_id = make_running_job(db, consultation_id)
    with pytest.raises(CheckViolation):
        db.execute(
            "UPDATE job SET params = %s::jsonb WHERE id = %s",
            ('"502 Bad Gateway while sending the prompt"', job_id),
        )
    db.rollback()


def test_an_unknown_job_records_nothing(db: psycopg.Connection[DictRow]) -> None:
    with pytest.raises(LeaseLostError):
        record_failure(db, Lease(uuid4(), "worker-1", 1), ErrorCode.WORKER_ERROR)
