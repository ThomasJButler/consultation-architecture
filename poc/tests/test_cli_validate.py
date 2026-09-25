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
