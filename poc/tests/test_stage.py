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

from consult.definition import Definition
from consult.inputs import Caps, InputError
from consult.responses import Responses
from consult.stage import StageError, stage, staging_table
from consult.validate import validate
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


def test_stage_refuses_a_header_over_sixty_three_bytes(
    db: psycopg.Connection[DictRow], tmp_path: Path
) -> None:
    # The validator blocks this first; stage is the backstop, so a caller
    # that skipped validation can't lose a column to Postgres's identifier
    # truncation. Refused before the consultation moves or a table exists.
    consultation_id = make_consultation(db, make_department(db))
    long_header = "email_" + "x" * 70
    path = tmp_path / "responses.csv"
    path.write_text(
        f"respondent_ref,{long_header},o_reason\nR-1,a@example.org,why\n", encoding="utf-8"
    )

    with pytest.raises(StageError) as refused:
        stage(db, consultation_id, path)

    assert "63" in str(refused.value) and "a@example.org" not in str(refused.value)
    status = db.execute(
        "SELECT status FROM consultation WHERE id = %s", (consultation_id,)
    ).fetchone()
    assert status == {"status": "draft"}
    tables = db.execute(
        "SELECT count(*) AS n FROM pg_tables WHERE schemaname = 'staging'"
    ).fetchone()
    assert tables == {"n": 0}


def test_stage_refuses_a_repeated_or_blank_header(
    db: psycopg.Connection[DictRow], tmp_path: Path
) -> None:
    # The validator blocks these first (test_validate.py); this is the
    # backstop, refused before the edge and before any table, with a count
    # and never a cell.
    consultation_id = make_consultation(db, make_department(db))
    path = tmp_path / "responses.csv"
    path.write_text(
        "respondent_ref,d_area,d_area,,o_reason\nR-1,Town,Suburbs,x,why\n", encoding="utf-8"
    )

    with pytest.raises(StageError) as refused:
        stage(db, consultation_id, path)

    assert "5 headers" in str(refused.value) and "Town" not in str(refused.value)
    status = db.execute(
        "SELECT status FROM consultation WHERE id = %s", (consultation_id,)
    ).fetchone()
    assert status == {"status": "draft"}
    tables = db.execute(
        "SELECT count(*) AS n FROM pg_tables WHERE schemaname = 'staging'"
    ).fetchone()
    assert tables == {"n": 0}


def test_the_validator_refuses_what_the_stager_cannot_hold(
    db: psycopg.Connection[DictRow], tmp_path: Path
) -> None:
    # Four files the header checks pass and a staging table can't hold: a
    # header named row_no, stage's own column; a NUL in a cell, which
    # Postgres text can't store; a NUL in two headers, where libpq ends the
    # identifier, so both would name one column "notes"; and 1,600
    # columns, one more than a table takes beside row_no. validate()
    # refuses each by a code and a count, never the value, and stage()
    # refuses the same if a caller hands it the file unvalidated. The
    # reader's width cap is raised to Excel's 16,384, as CONSULT_MAX_COLUMNS
    # can raise it, so the refusal is the stager's limit and not the reader's.
    wide = Caps(max_columns=16_384)
    reserved = tmp_path / "reserved.csv"
    reserved.write_text("respondent_ref,row_no,o_reason\nR-1,7,why\n", encoding="utf-8")
    nul_cell = tmp_path / "nul-cell.csv"
    nul_cell.write_text("respondent_ref,o_reason\nR-1,fine\nR-2,hid\x00den\n", encoding="utf-8")
    nul_header = tmp_path / "nul-header.csv"
    nul_header.write_text("notes\x00x,notes\x00y\nhidden,hidden\n", encoding="utf-8")
    columns = tmp_path / "columns.csv"
    columns.write_text(
        ",".join(f"h{n}" for n in range(1, 1_601)) + "\n" + ",".join(["hidden"] * 1_600) + "\n",
        encoding="utf-8",
    )
    # The code and the count as the command line prints them: the header's
    # position, the row the NUL is on (the header is row 1), the width.
    cases = [
        (reserved, "reserved_header (2)"),
        (nul_cell, "nul_character (3)"),
        (nul_header, "nul_character (1)"),
        (columns, "too_many_columns (1600)"),
    ]
    department_id = make_department(db)

    for path, refusal in cases:
        with pytest.raises(InputError) as refused:
            validate(Definition(demographic=(), closed=(), open=()), Responses(path, wide))
        assert str(refused.value) == refusal, path.name

        consultation_id = make_consultation(db, department_id)
        with pytest.raises(InputError) as refused, db.transaction():
            stage(db, consultation_id, path, wide)
        assert str(refused.value) == refusal, path.name
        # What the caller's rollback leaves: the consultation where it was
        # and no table holding the file.
        status = db.execute(
            "SELECT status FROM consultation WHERE id = %s", (consultation_id,)
        ).fetchone()
        assert status == {"status": "draft"}
        tables = db.execute(
            "SELECT count(*) AS n FROM pg_tables WHERE schemaname = 'staging'"
        ).fetchone()
        assert tables == {"n": 0}
