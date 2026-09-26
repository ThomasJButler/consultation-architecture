"""What `consult ingest` promises: a spreadsheet into a schema full of rows
in one command, committed, with counts and ids printed and nothing else.

Exit 0 when the consultation is processing, 1 when the validator found an
error or ingest refused, 2 when the file was refused before it was read.
Whatever the outcome, the output carries counts, row numbers and ids and
never an answer, an email address or a respondent id from the file.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
from psycopg.errors import UniqueViolation
from psycopg.rows import DictRow

from consult.cli import main
from consult.config import Settings
from consult.inputs import Caps
from tests.test_definition import GOOD, write_workbook
from tests.test_ingest import staging_tables

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RESPONSES = str(FIXTURES / "responses.csv")
DEFINITION = str(FIXTURES / "definition.xlsx")
ARGS = ["--name", "Riverside cycle route", "--department", "Department of Fictional Affairs"]


def test_the_ingest_command_runs_the_fixtures_end_to_end(
    db: psycopg.Connection[DictRow], db_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["ingest", RESPONSES, "--definition", DEFINITION, *ARGS], settings=db_settings)

    out = capsys.readouterr().out
    assert code == 0
    # Counts and ids (docs/02, step 3a; plans/PR-06 section 1)...
    for expected in (
        "240 rows staged",
        "240 respondents",
        "240 identity rows",
        "2 jobs",
        "duplicate answers",
        "duplicate respondents",
    ):
        assert expected in out
    found = re.search(r"consultation ([0-9a-f-]{36})", out)
    assert found is not None
    # ...and not one value from the file.
    assert "towpath" not in out and "example.org" not in out and "R-0001" not in out
    # Committed on its own connection, so this one sees it.
    consultation = db.execute(
        """
        SELECT c.status, d.name AS department,
               (SELECT count(*) FROM respondent r WHERE r.consultation_id = c.id) AS respondents,
               (SELECT count(*) FROM job j WHERE j.consultation_id = c.id AND j.status = 'pending')
                   AS jobs
          FROM consultation c JOIN department d ON d.id = c.department_id
         WHERE c.id = %s
        """,
        (found.group(1),),
    ).fetchone()
    assert consultation == {
        "status": "processing",
        "department": "Department of Fictional Affairs",
        "respondents": 240,
        "jobs": 2,
    }
    assert staging_tables(db) == []


def test_the_ingest_command_writes_nothing_when_the_file_is_not_fit(
    db: psycopg.Connection[DictRow],
    db_settings: Settings,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # A definition error: exit 1, the errors printed, no consultation row.
    rows = {**GOOD, "Demographic questions": [("d_gone", "Gone?")]}
    bad = write_workbook(tmp_path / "definition.xlsx", rows)
    assert main(["ingest", RESPONSES, "--definition", str(bad), *ARGS], settings=db_settings) == 1
    out = capsys.readouterr().out
    assert "errors: " in out and "d_gone" in out
    # A refusal before the read: exit 2, the reason and the number.
    small = replace(db_settings, caps=Caps(max_rows=5))
    assert main(["ingest", RESPONSES, "--definition", DEFINITION, *ARGS], settings=small) == 2
    assert "refused: too_many_rows (5)" in capsys.readouterr().out
    written = db.execute(
        "SELECT (SELECT count(*) FROM consultation) AS consultations, (SELECT count(*) FROM department) AS departments"
    ).fetchone()
    assert written == {"consultations": 0, "departments": 0}


def test_a_department_name_names_one_department(
    db: psycopg.Connection[DictRow], db_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    # The command finds or creates the department by name, so the name has
    # to be unique or two runs racing on a new name would each make one and
    # every later consultation would land on whichever row came back first.
    with pytest.raises(UniqueViolation), db.transaction():
        db.execute("INSERT INTO department (name) VALUES (%s), (%s)", ("Twice", "Twice"))
    assert main(["ingest", RESPONSES, "--definition", DEFINITION, *ARGS], settings=db_settings) == 0
    second = ["--name", "A second consultation", *ARGS[2:]]
    assert (
        main(["ingest", RESPONSES, "--definition", DEFINITION, *second], settings=db_settings) == 0
    )
    capsys.readouterr()
    counts = db.execute(
        "SELECT (SELECT count(*) FROM department) AS departments, (SELECT count(*) FROM consultation) AS consultations"
    ).fetchone()
    assert counts == {"departments": 1, "consultations": 2}
