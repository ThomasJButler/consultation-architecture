"""What the vault promises (docs/06, section 2.4 as corrected; THREAT_MODEL.md, row 6).

Identity columns land in `vault.respondent_identity` as the ingest role and
nowhere else, and the pipeline role has no grant on the schema, so a
SELECT as that role is refused by Postgres and not by convention. The
other half of the promise, that nothing on the pipeline path names the
schema, is a repo rule in test_repo_rules.py.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import DictRow

from consult.ingest import ingest
from consult.store import as_role
from tests.pipeline import fixture_rows, staged_fixture

pytestmark = pytest.mark.db


def test_identity_columns_go_to_the_vault_and_nowhere_else(db: psycopg.Connection[DictRow]) -> None:
    rows = fixture_rows()
    staged = staged_fixture(db)

    result = ingest(db, staged.consultation_id)

    # One vault row per respondent per identity column, the value as the
    # file had it, under the consultation's department.
    expected = {no: row["email"] for no, row in enumerate(rows, start=2)}
    assert all("@" in value for value in expected.values())
    vault = db.execute(
        """
        SELECT r.source_row_no, v.column_ref, v.value_text, v.department_id = r.department_id AS scoped
          FROM vault.respondent_identity v JOIN respondent r ON r.id = v.respondent_id
         WHERE r.consultation_id = %s
        """,
        (staged.consultation_id,),
    ).fetchall()
    assert {v["source_row_no"]: v["value_text"] for v in vault} == expected
    assert {(v["column_ref"], v["scoped"]) for v in vault} == {("email", True)}
    assert result.vault_rows == 240
    # And nowhere else: no answer row, nothing in attrs, nothing in a
    # question's text (docs/04, section 1).
    leaks = db.execute(
        """
        SELECT (SELECT count(*) FROM answer
                 WHERE consultation_id = %(id)s AND value_text LIKE '%%@%%') AS answers,
               (SELECT count(*) FROM respondent
                 WHERE consultation_id = %(id)s AND attrs::text LIKE '%%@%%') AS attrs,
               (SELECT count(*) FROM question
                 WHERE consultation_id = %(id)s AND question_text LIKE '%%@%%') AS questions
        """,
        {"id": staged.consultation_id},
    ).fetchone()
    assert leaks == {"answers": 0, "attrs": 0, "questions": 0}


def test_the_pipeline_role_cannot_read_the_vault(db: psycopg.Connection[DictRow]) -> None:
    staged = staged_fixture(db)
    ingest(db, staged.consultation_id)

    with as_role(db, "consult_pipeline"):
        # The pipeline's own tables read as they should...
        answers = db.execute(
            "SELECT count(*) AS n FROM answer WHERE consultation_id = %s", (staged.consultation_id,)
        ).fetchone()
        assert answers is not None and answers["n"] > 0
        # ...and the vault refuses the role at the schema, before the table:
        # no USAGE grant, so "identifiers never in a prompt" holds even when
        # the code gets it wrong (ADR-004). The savepoint keeps the
        # connection usable for the RESET ROLE that follows.
        with pytest.raises(InsufficientPrivilege) as refused, db.transaction():
            db.execute("SELECT count(*) FROM vault.respondent_identity")
        assert "permission denied for schema vault" in str(refused.value)
        with pytest.raises(InsufficientPrivilege), db.transaction():
            db.execute(
                "INSERT INTO vault.respondent_identity SELECT department_id, id, 'x', 'y' FROM respondent"
            )
