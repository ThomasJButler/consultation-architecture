"""What the validator promises: the report docs/02 section 3.2 describes.

Errors block, warnings carry a resolution, every demographic and closed
column lists its distinct values with counts so spelling variants surface
before spend, and no row is ever dropped. The fixtures carry the awkward
cases on purpose (scripts/make_fixture_data.py), so the whole report for
them is pinned here, with the expected counts read straight from the CSV
so the test can't drift from the fixture.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from consult.definition import (
    ClosedQuestion,
    Definition,
    DemographicQuestion,
    OpenQuestion,
    ResponseType,
    read_definition,
)
from consult.responses import Responses
from consult.validate import (
    ColumnKind,
    Report,
    Resolution,
    Warning,
    WarningKind,
    validate,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOT_ANSWERED = {"", "-"}


def fixture_rows() -> list[dict[str, str]]:
    with (FIXTURES / "responses.csv").open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def fixture_report() -> Report:
    return validate(
        read_definition(FIXTURES / "definition.xlsx"), Responses(FIXTURES / "responses.csv")
    )


def warnings_of(report: Report, kind: WarningKind) -> list[Warning]:
    return [warning for warning in report.warnings if warning.kind is kind]


def test_the_validator_reports_the_fixtures_as_designed() -> None:
    rows = fixture_rows()
    report = fixture_report()

    assert report.errors == ()
    assert report.row_count == len(rows) == 240
    assert Counter(warning.kind for warning in report.warnings) == {
        WarningKind.UNKNOWN_VALUE: 1,
        WarningKind.NOT_APPLICABLE: 1,
        WarningKind.OPTIONS_NEVER_APART: 1,
        WarningKind.UNMATCHED_HEADER: 3,
    }

    (unsure,) = warnings_of(report, WarningKind.UNKNOWN_VALUE)
    assert (unsure.column_ref, unsure.value, unsure.count) == ("c_route", "Unsure", 14)
    # The header is row 1, so the first fixture row is row 2.
    assert (
        unsure.example_rows
        == tuple(no for no, row in enumerate(rows, start=2) if row["c_route"] == "Unsure")[:5]
    )
    assert unsure.resolutions == (
        Resolution.MAP_TO_OPTION,
        Resolution.ADD_AS_OPTION,
        Resolution.TREAT_AS_NOT_ANSWERED,
    )
    assert unsure.default is Resolution.MAP_TO_OPTION

    (not_applicable,) = warnings_of(report, WarningKind.NOT_APPLICABLE)
    assert not_applicable.column_ref == "d_commute"
    assert not_applicable.count == sum(row["d_commute"] == "N/A" for row in rows)
    assert not_applicable.resolutions == (
        Resolution.KEEP_AS_VALUE,
        Resolution.TREAT_AS_NOT_ANSWERED,
    )
    assert not_applicable.default is Resolution.KEEP_AS_VALUE

    by_ref = {column.column_ref: column for column in report.columns}
    assert [column.column_ref for column in report.columns] == list(rows[0])
    area = by_ref["d_area"]
    assert area.kind is ColumnKind.DEMOGRAPHIC
    assert area.not_answered == sum(row["d_area"] in NOT_ANSWERED for row in rows)
    assert area.answered == len(rows) - area.not_answered
    assert dict(area.values) == Counter(
        row["d_area"] for row in rows if row["d_area"] not in NOT_ANSWERED
    )
    assert area.values == tuple(sorted(area.values, key=lambda pair: (-pair[1], pair[0])))
    # N/A is a real value on a demographic column until the configure step
    # says otherwise, so it's counted with the rest.
    assert dict(by_ref["d_commute"].values)["N/A"] == not_applicable.count

    modes = by_ref["c_modes"]
    assert modes.kind is ColumnKind.CLOSED
    assert dict(modes.values)["Cycle"] == sum("Cycle" in row["c_modes"].split(", ") for row in rows)

    reason = by_ref["o_reason"]
    assert reason.kind is ColumnKind.OPEN
    assert reason.values == ()
    # N/A on an open question is not answered (docs/02, section 3.2).
    assert reason.not_answered == sum(row["o_reason"] in NOT_ANSWERED | {"N/A"} for row in rows)
    assert report.open_answer_count == sum(
        row[ref] not in NOT_ANSWERED | {"N/A"} for row in rows for ref in ("o_reason", "o_safety")
    )


def test_a_missing_column_the_definition_references_is_an_error() -> None:
    definition = Definition(
        demographic=(
            DemographicQuestion("d_area", "Area?"),
            DemographicQuestion("d_gone", "Gone?"),
        ),
        closed=(ClosedQuestion("c_lost", "Lost?", ResponseType.SINGLE_SELECT, ("Yes", "No")),),
        open=(OpenQuestion("o_reason", "Why?", None),),
    )
    report = validate(definition, Responses(FIXTURES / "responses.csv"))
    assert len(report.errors) == 2
    assert any("d_gone" in error for error in report.errors)
    assert any("c_lost" in error for error in report.errors)
    # An error blocks (docs/02, section 3.2), but the rest of the report is
    # still produced so the configure screen shows everything at once.
    assert report.row_count == 240


def test_a_duplicated_respondent_id_is_a_warning_with_two_resolutions(tmp_path: Path) -> None:
    path = tmp_path / "responses.csv"
    path.write_text(
        "respondent_ref,d_area,o_reason\n"
        "R-1,Villages,one\n"
        "R-2,Suburbs,two\n"
        "R-1,Villages,three\n"
        "R-3,Suburbs,four\n"
        "R-1,Villages,five\n"
        "-,Suburbs,six\n"
        "-,Suburbs,seven\n",
        encoding="utf-8",
    )
    definition = Definition(
        demographic=(DemographicQuestion("d_area", "Area?"),),
        closed=(),
        open=(OpenQuestion("o_reason", "Why?", None),),
    )
    report = validate(definition, Responses(path))
    (duplicate,) = warnings_of(report, WarningKind.DUPLICATE_RESPONDENT_ID)
    assert (duplicate.column_ref, duplicate.value, duplicate.count) == ("respondent_ref", "R-1", 3)
    assert duplicate.example_rows == (2, 4, 6)
    assert duplicate.resolutions == (Resolution.IGNORE_COLUMN, Resolution.KEEP_FIRST_BLANK_REST)
    assert duplicate.default is Resolution.KEEP_FIRST_BLANK_REST
    # Blank ids aren't duplicates of each other, and no row is dropped.
    assert report.row_count == 7


def test_options_that_never_appear_apart_are_flagged_to_merge() -> None:
    (never_apart,) = warnings_of(fixture_report(), WarningKind.OPTIONS_NEVER_APART)
    assert never_apart.column_ref == "c_modes"
    assert never_apart.value == "Wheelchair, mobility scooter or similar"
    assert never_apart.count == sum(
        "Wheelchair, mobility scooter or similar" in row["c_modes"] for row in fixture_rows()
    )
    assert never_apart.resolutions == (Resolution.MERGE_OPTIONS,)
    assert never_apart.default is Resolution.MERGE_OPTIONS


def test_a_header_no_sheet_mentions_gets_a_role_prompt() -> None:
    unmatched = {
        w.column_ref: w for w in warnings_of(fixture_report(), WarningKind.UNMATCHED_HEADER)
    }
    assert set(unmatched) == {"respondent_ref", "email", "notes_internal"}
    assert unmatched["respondent_ref"].default is Resolution.ROLE_RESPONDENT_ID
    assert unmatched["email"].default is Resolution.ROLE_IDENTITY
    assert unmatched["notes_internal"].default is Resolution.ROLE_IGNORE
    for warning in unmatched.values():
        assert warning.resolutions == (
            Resolution.ROLE_RESPONDENT_ID,
            Resolution.ROLE_IDENTITY,
            Resolution.ROLE_IGNORE,
        )
        assert warning.value is None


def test_a_header_named_like_an_id_gets_the_role_however_it_is_spelt() -> None:
    # Survey tools export headers with spaces and capitals, and a column
    # that says both "email" and "id" is an identity column first: it goes
    # to the vault rather than becoming the key (docs/02, section 3.2).
    from consult.validate import suggested_role

    assert suggested_role("Response ID") is Resolution.ROLE_RESPONDENT_ID
    assert suggested_role("RespondentID") is Resolution.ROLE_RESPONDENT_ID
    assert suggested_role("ref") is Resolution.ROLE_RESPONDENT_ID
    assert suggested_role("Email Address") is Resolution.ROLE_IDENTITY
    assert suggested_role("email_id") is Resolution.ROLE_IDENTITY
    assert suggested_role("Full name") is Resolution.ROLE_IDENTITY
    assert suggested_role("notes_internal") is Resolution.ROLE_IGNORE


def test_every_id_like_column_gets_its_own_duplicate_check(tmp_path: Path) -> None:
    # Two candidates for the respondent id, and the one the reviewer might
    # pick at configure time has the duplicates. Both are checked.
    path = tmp_path / "responses.csv"
    path.write_text(
        "respondent_ref,submission_id,o_reason\nR-1,S-1,one\nR-2,S-1,two\nR-3,S-1,three\n",
        encoding="utf-8",
    )
    definition = Definition(
        demographic=(), closed=(), open=(OpenQuestion("o_reason", "Why?", None),)
    )
    report = validate(definition, Responses(path))
    (duplicate,) = warnings_of(report, WarningKind.DUPLICATE_RESPONDENT_ID)
    assert (duplicate.column_ref, duplicate.value, duplicate.count) == ("submission_id", "S-1", 3)


def test_two_options_chosen_together_once_are_not_flagged_to_merge(tmp_path: Path) -> None:
    # The never-apart warning is for a comma option the workbook split in
    # two, and those halves sit next to each other in the option list. Two
    # options from elsewhere in the list that one respondent picked
    # together are not that, however often it happens.
    path = tmp_path / "responses.csv"
    path.write_text("c_modes\nRun, Push a pram\nCycle\nWalk\nRun, Push a pram\n", encoding="utf-8")
    definition = Definition(
        demographic=(),
        closed=(
            ClosedQuestion(
                "c_modes",
                "How?",
                ResponseType.MULTI_SELECT,
                ("Cycle", "Walk", "Run", "Push a pram"),
            ),
        ),
        open=(),
    )
    assert warnings_of(validate(definition, Responses(path)), WarningKind.OPTIONS_NEVER_APART) == []

    # The same two, adjacent in the option list as a split comma option
    # would be, are flagged.
    definition = Definition(
        demographic=(),
        closed=(
            ClosedQuestion(
                "c_modes",
                "How?",
                ResponseType.MULTI_SELECT,
                ("Cycle", "Run", "Push a pram", "Walk"),
            ),
        ),
        open=(),
    )
    (flagged,) = warnings_of(validate(definition, Responses(path)), WarningKind.OPTIONS_NEVER_APART)
    assert (flagged.value, flagged.count) == ("Run, Push a pram", 2)
