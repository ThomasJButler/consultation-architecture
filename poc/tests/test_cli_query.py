"""What `consult query` and `consult export` promise: docs/02 screen 4's
per-question dashboard and step 12's workbook, both printed rather than
paged, and neither printing an answer's text (the objective in
plans/PR-09-poc-query-export-cli.md section 1).

Driven through the fixtures taken to `ready` by the worker commands, the
same objective test_cli_worker.py drives: ingest, two `worker --once`
runs, two sign-offs, two more `worker --once` runs.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from openpyxl import load_workbook
from psycopg.rows import DictRow

from consult import query
from consult.cli import main
from consult.config import Settings
from consult.store import PIPELINE_ROLE
from tests.test_cli_themes import ANSWER_FRAGMENTS, ingested

pytestmark = pytest.mark.db


def test_the_query_and_export_commands(
    db: psycopg.Connection[DictRow],
    db_settings: Settings,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    ingested(db, db_settings)
    question_ids = [
        row["id"]
        for row in db.execute(
            "SELECT id FROM question WHERE kind = 'open' ORDER BY ordinal"
        ).fetchall()
    ]
    for _ in range(2):
        assert main(["worker", "--once", "--worker", "w1"], settings=db_settings) == 0
    reviewer = str(uuid4())
    for question_id in question_ids:
        assert (
            main(
                ["sign-off", str(question_id), "--reviewer", reviewer, "--expect-version", "0"],
                settings=db_settings,
            )
            == 0
        )
    for _ in range(2):
        assert main(["worker", "--once", "--worker", "w1"], settings=db_settings) == 0
    reason_row = db.execute("SELECT id FROM question WHERE column_ref = 'o_reason'").fetchone()
    consultation_row = db.execute("SELECT id FROM consultation").fetchone()
    assert reason_row is not None
    assert consultation_row is not None
    reason_id = reason_row["id"]
    consultation_id = consultation_row["id"]
    capsys.readouterr()

    # Hand count from responses.csv, the same as test_query_db.py's own
    # against d_area=Villages: 17 of the 240 respondents give a non-blank,
    # non-duplicate o_reason answer there, and the related c_route
    # distribution among them is 8 Support, 4 Oppose, 4 Not sure. The fake
    # tags every answer with the first key of the frozen shortlist,
    # ACCESS (mapping._shortlist orders by key; fake_model.good_assignments
    # takes theme_keys[0]), so ACCESS's count is the denominator under any
    # filter that still selects some of those 17.
    code = main(
        [
            "query",
            str(reason_id),
            "--filter",
            "attr:d_area=Villages",
            "--filter",
            "theme:ACCESS",
        ],
        settings=db_settings,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "of 17 respondents who answered" in out
    assert re.search(r"^\s+ACCESS\s+Access\s+17\s+100\.0%$", out, re.M)
    assert "Support 8" in out and "Oppose 4" in out and "Not sure 4" in out
    for fragment in ANSWER_FRAGMENTS:
        assert fragment not in out

    # A malformed filter is refused by its code, before any query runs,
    # and the value that triggered it never reaches the line.
    capsys.readouterr()
    code = main(
        ["query", str(reason_id), "--filter", "other:o_safety=a-secret-value"],
        settings=db_settings,
    )
    out = capsys.readouterr().out
    assert code == 2
    assert "refused: malformed_other" in out
    assert "a-secret-value" not in out

    out_path = tmp_path / "consult.xlsx"
    capsys.readouterr()

    code = main(["export", str(consultation_id), "--out", str(out_path)], settings=db_settings)

    out = capsys.readouterr().out
    assert code == 0
    assert str(out_path) in out
    assert "240 respondents" in out and "480 answers" in out and "4 sheets" in out
    assert re.search(r"\d+ tags", out)
    for fragment in ANSWER_FRAGMENTS:
        assert fragment not in out

    # The fixture's one formula-trigger answer reads back prefixed, and
    # every cell in the workbook is a text cell (test_export.py's own
    # proof, read back here through the command rather than the function).
    workbook = load_workbook(out_path)
    responses = workbook["Responses"]
    header = [cell.value for cell in next(responses.iter_rows(max_row=1))]
    index = {name: i for i, name in enumerate(header)}
    rows = list(responses.iter_rows(min_row=2))
    by_ref = {row[index["respondent_ref"]].value: row for row in rows}
    assert by_ref["R-0057"][index["o_safety"]].value == "'=1+1"
    for sheet in workbook.worksheets:
        for sheet_row in sheet.iter_rows():
            for cell in sheet_row:
                if cell.value not in (None, ""):
                    assert cell.data_type == "s", (sheet.title, cell.coordinate)


def test_the_query_command_reads_as_the_pipeline_role(
    db: psycopg.Connection[DictRow],
    db_settings: Settings,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stand-in for the query command's first read, as
    # test_cli_themes.py's test_run_job_works_as_the_pipeline_role stands
    # in for the worker's own call, wrapped rather than replaced so the
    # rest of the command still runs for real.
    ingested(db, db_settings)
    for _ in range(2):
        assert main(["worker", "--once", "--worker", "w1"], settings=db_settings) == 0
    reviewer = str(uuid4())
    open_ids = [
        row["id"]
        for row in db.execute(
            "SELECT id FROM question WHERE kind = 'open' ORDER BY ordinal"
        ).fetchall()
    ]
    for open_id in open_ids:
        assert (
            main(
                ["sign-off", str(open_id), "--reviewer", reviewer, "--expect-version", "0"],
                settings=db_settings,
            )
            == 0
        )
    for _ in range(2):
        assert main(["worker", "--once", "--worker", "w1"], settings=db_settings) == 0
    reason_row = db.execute("SELECT id FROM question WHERE column_ref = 'o_reason'").fetchone()
    assert reason_row is not None
    reason_id = reason_row["id"]
    capsys.readouterr()

    seen: list[str] = []
    original = query.theme_table

    def recording(
        conn: psycopg.Connection[DictRow],
        question_id: UUID,
        filter: query.Filter,
        *,
        department_id: UUID,
    ) -> query.ThemeTable:
        row = conn.execute("SELECT current_user AS who").fetchone()
        assert row is not None
        seen.append(str(row["who"]))
        return original(conn, question_id, filter, department_id=department_id)

    monkeypatch.setattr(query, "theme_table", recording)

    assert main(["query", str(reason_id)], settings=db_settings) == 0
    capsys.readouterr()

    # docs/06 section 2.4's backstop is the pipeline role's missing grant
    # on the vault: the query path names no vault table (query.py's own
    # module docstring), so it should read as the role that can't reach
    # the vault even if the code above it gets that wrong, not as the
    # export role, whose vault grant this path never needs.
    assert seen == [PIPELINE_ROLE]


def test_the_query_command_holds_to_the_named_department(
    db: psycopg.Connection[DictRow],
    db_settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # docs/06 section 2: the department is the caller's. Named with
    # --department, it holds the query to that department, so another
    # department's question reaches no respondent; left out, the command
    # takes the question's own. Ingested only: the denominator needs no
    # theme, and it's test_query_db.py's hand count of 74 o_reason rows.
    ingested(db, db_settings)
    reason_row = db.execute(
        "SELECT id, department_id FROM question WHERE column_ref = 'o_reason'"
    ).fetchone()
    assert reason_row is not None
    reason_id = reason_row["id"]
    capsys.readouterr()

    def first_line(*extra: str) -> str:
        assert main(["query", str(reason_id), *extra], settings=db_settings) == 0
        return capsys.readouterr().out.splitlines()[0]

    elsewhere = first_line("--department", str(uuid4()))
    assert elsewhere == f"question {reason_id}: of 0 respondents who answered"
    named = first_line("--department", str(reason_row["department_id"]))
    assert named == f"question {reason_id}: of 74 respondents who answered"
    assert first_line() == named
