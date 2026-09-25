"""What the stage step promises (docs/02, step 2; docs/04, section 2).

The file is copied into one logged table per upload in the `staging`
schema, named by the consultation id, every column text plus the file's
row number. Logged, not unlogged, because the table lives across the
human configure step and Postgres truncates an unlogged table after a
crash (docs/01, section 6). The consultation records the file's sha256
and row count and moves to staged.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import DictRow

from consult.stage import stage, staging_table
from tests.rows import make_consultation, make_department

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_stage_copies_the_file_into_a_logged_staging_table(db: psycopg.Connection[DictRow]) -> None:
    consultation_id = make_consultation(db, make_department(db))
    path = FIXTURES / "responses.csv"

    staged = stage(db, consultation_id, path)

    assert staged.rows == 240
    assert staged.sha256 == hashlib.sha256(path.read_bytes()).digest()
    table = db.execute(
        """
        SELECT n.nspname AS schema, c.relpersistence AS persistence
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relname = %s
        """,
        (str(consultation_id),),
    ).fetchone()
    assert table == {"schema": "staging", "persistence": "p"}
    first = db.execute(
        sql.SQL("SELECT row_no, respondent_ref, o_reason FROM {} ORDER BY row_no LIMIT 1").format(
            staging_table(consultation_id)
        )
    ).fetchone()
    assert first is not None
    assert (first["row_no"], first["respondent_ref"]) == (2, "R-0001")
    count = db.execute(
        sql.SQL("SELECT count(*) AS n FROM {}").format(staging_table(consultation_id))
    ).fetchone()
    assert count == {"n": 240}
    consultation = db.execute(
        "SELECT status, upload_sha256, row_count FROM consultation WHERE id = %s",
        (consultation_id,),
    ).fetchone()
    assert consultation == {"status": "staged", "upload_sha256": staged.sha256, "row_count": 240}
