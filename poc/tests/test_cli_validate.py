"""What `consult validate` promises: the report, printed, and an exit code
that says whether the file can go on to configure.

0 means no errors, 1 means errors that block, 2 means the file was refused
before it was read. The printed report carries column names, counts, row
numbers and the distinct values of demographic and closed columns; an
open answer never appears in it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from consult import report
from consult.cli import main
from consult.config import Settings
from consult.inputs import Caps
from tests.test_definition import GOOD, write_workbook

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RESPONSES = str(FIXTURES / "responses.csv")
DEFINITION = str(FIXTURES / "definition.xlsx")
SETTINGS = Settings(db_host="h", db_port=1, db_name="d", db_user="u", db_password="p")


def test_validate_prints_the_report_and_exits_zero_on_the_fixtures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["validate", RESPONSES, "--definition", DEFINITION], settings=SETTINGS) == 0
    out = capsys.readouterr().out
    assert "240 rows" in out
    assert "errors: none" in out
    assert "unknown_value" in out and "c_route" in out and "Unsure" in out and "x14" in out
    assert "not_applicable" in out and "d_commute" in out
    assert "options_never_apart" in out and "Wheelchair, mobility scooter or similar" in out
    assert "respondent_ref" in out and "role_respondent_id (default)" in out
    assert "d_area" in out and "Town centre" in out
    assert "open answers:" in out and "tokens" in out and "cached" in out
    assert "150 tokens per open answer" in out
    # The report describes the file; it never quotes an open answer.
    assert "towpath" not in out and "Mill Lane" not in out


def test_validate_exits_one_when_an_error_blocks(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    rows = {**GOOD, "Demographic questions": [("d_gone", "Gone?")]}
    definition = write_workbook(tmp_path / "definition.xlsx", rows)
    assert main(["validate", RESPONSES, "--definition", str(definition)], settings=SETTINGS) == 1
    out = capsys.readouterr().out
    assert "errors: " in out and "d_gone" in out

    rows = {**GOOD, "Closed questions": [("c_route", "Support?", "number", "1, 2")]}
    definition = write_workbook(tmp_path / "bad-type.xlsx", rows)
    assert main(["validate", RESPONSES, "--definition", str(definition)], settings=SETTINGS) == 1
    assert "single-select, likert-5, multi-select" in capsys.readouterr().out


def test_validate_exits_two_when_the_file_is_refused(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    small = Settings(
        db_host="h", db_port=1, db_name="d", db_user="u", db_password="p", caps=Caps(max_rows=5)
    )
    assert main(["validate", RESPONSES, "--definition", DEFINITION], settings=small) == 2
    out = capsys.readouterr().out
    assert "refused: too_many_rows (5)" in out
    assert "R-0001" not in out

    plain = tmp_path / "plain.xlsx"
    plain.write_text("not a workbook", encoding="utf-8")
    assert main(["validate", RESPONSES, "--definition", str(plain)], settings=SETTINGS) == 2
    assert "refused: not_a_zip" in capsys.readouterr().out


def test_validate_can_print_the_report_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["validate", RESPONSES, "--definition", DEFINITION, "--json"], settings=SETTINGS) == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["row_count"] == 240
    assert report["errors"] == []
    assert {w["kind"] for w in report["warnings"]} >= {"unknown_value", "unmatched_header"}
    assert report["estimate"]["open_answers"] == report["open_answer_count"]
    assert report["estimate"]["pence_cached"] > 0


def test_a_cell_cannot_forge_a_line_of_the_report(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # A newline typed into a free-text field, or an escape sequence in a
    # crafted file, must not become a second line or a terminal command in
    # the report the configure step reads (THREAT_MODEL.md, rows 12 and 14).
    responses = tmp_path / "responses.csv"
    responses.write_text(
        "d_area,c_route,o_reason\n"
        '"Villages\nerrors: 1\n  FORGED",Support,why\n'
        'Suburbs,"Oppose\x1b[2J\x1b[H",why\n',
        encoding="utf-8",
    )
    rows = {
        **GOOD,
        "Open questions": [("o_reason", "Why?", "-")],
        "Closed questions": [("c_route", "Support?", "single-select", "Support, Oppose")],
    }
    definition = write_workbook(tmp_path / "definition.xlsx", rows)
    assert (
        main(["validate", str(responses), "--definition", str(definition)], settings=SETTINGS) == 0
    )
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "FORGED" in out
    assert "\n  FORGED" not in out
    # One real "errors:" line; the forged one is text inside a value now.
    assert out.count("\nerrors: ") == 1
    # Unicode format characters are escaped the same way: a bidirectional
    # override or a zero-width space in a cell can't make a line read as
    # something it isn't (the security review of PR-07).
    assert report.shown("safe\u202eelbatable\u200b") == "safe\\u202eelbatable\\u200b"


def test_a_definition_problem_cannot_forge_a_line_either(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # The workbook is the department's own file, but the same rule holds:
    # what a cell says is shown as text, never as a line of the report.
    rows = {**GOOD, "Closed questions": [("c_route\nerrors: 9", "Support?", "number", "1, 2")]}
    definition = write_workbook(tmp_path / "definition.xlsx", rows)
    assert main(["validate", RESPONSES, "--definition", str(definition)], settings=SETTINGS) == 1
    out = capsys.readouterr().out
    assert out.count("\nerrors: ") == 0
    assert out.startswith("errors: ")
    assert "\\n" in out
