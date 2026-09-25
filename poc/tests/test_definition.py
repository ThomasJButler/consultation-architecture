"""What the definition workbook parser promises.

The format is docs/00's: three sheets with fixed headers, options joined by
commas, `-` or blank in `related_closed_column` for no related question,
and a fixed vocabulary of three response types spelt as docs/02 section
3.2 spells them. Every problem the parser finds is reported at once, as a
list, so the configure step can show them all rather than one per upload.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from openpyxl import Workbook

from consult.definition import (
    SHEETS,
    ClosedQuestion,
    Definition,
    DefinitionError,
    DemographicQuestion,
    OpenQuestion,
    ResponseType,
    read_definition,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "definition.xlsx"

Rows = Mapping[str, Sequence[Sequence[str]]]


def write_workbook(
    path: Path, rows: Rows, *, headers: Mapping[str, Sequence[str]] = SHEETS
) -> Path:
    """A workbook with the three sheets, their headers, and the rows given."""
    workbook = Workbook()
    del workbook["Sheet"]
    for sheet, header in headers.items():
        worksheet = workbook.create_sheet(sheet)
        worksheet.append(list(header))
        for row in rows.get(sheet, ()):
            worksheet.append(list(row))
    workbook.save(path)
    return path


GOOD: Rows = {
    "Demographic questions": [("d_area", "Which part of the district?")],
    "Closed questions": [
        ("c_route", "Support the route?", "single-select", "Support, Oppose, Not sure"),
        ("c_modes", "How would you use it?", "multi-select", "Cycle, Walk"),
        ("c_safety", "How safe?", "likert-5", "Very unsafe, Unsafe, Neither, Safe, Very safe"),
    ],
    "Open questions": [
        ("o_reason", "You answered '{answer}'. Why?", "c_route"),
        ("o_safety", "What would make it safer?", "-"),
        ("o_other", "Anything else?", ""),
    ],
}


def test_the_definition_workbook_parses_into_three_kinds_of_question(tmp_path: Path) -> None:
    definition = read_definition(write_workbook(tmp_path / "definition.xlsx", GOOD))

    assert definition == Definition(
        demographic=(DemographicQuestion("d_area", "Which part of the district?"),),
        closed=(
            ClosedQuestion(
                "c_route",
                "Support the route?",
                ResponseType.SINGLE_SELECT,
                ("Support", "Oppose", "Not sure"),
            ),
            ClosedQuestion(
                "c_modes", "How would you use it?", ResponseType.MULTI_SELECT, ("Cycle", "Walk")
            ),
            ClosedQuestion(
                "c_safety",
                "How safe?",
                ResponseType.LIKERT_5,
                ("Very unsafe", "Unsafe", "Neither", "Safe", "Very safe"),
            ),
        ),
        open=(
            OpenQuestion("o_reason", "You answered '{answer}'. Why?", "c_route"),
            OpenQuestion("o_safety", "What would make it safer?", None),
            OpenQuestion("o_other", "Anything else?", None),
        ),
    )
    assert definition.column_refs == (
        "d_area",
        "c_route",
        "c_modes",
        "c_safety",
        "o_reason",
        "o_safety",
        "o_other",
    )
    assert definition.closed_by_ref["c_modes"].options == ("Cycle", "Walk")


def test_the_fixture_definition_reads_as_the_generator_wrote_it() -> None:
    definition = read_definition(FIXTURE)
    assert [q.column_ref for q in definition.demographic] == ["d_area", "d_commute", "d_age"]
    assert [q.response_type for q in definition.closed] == [
        ResponseType.SINGLE_SELECT,
        ResponseType.MULTI_SELECT,
        ResponseType.LIKERT_5,
    ]
    # The workbook can't express an option with a comma in it, so the split
    # gives one option too many here; the validator's job is to notice.
    assert len(definition.closed_by_ref["c_modes"].options) == 6
    assert [q.related_closed_column for q in definition.open] == ["c_route", None]


def test_a_related_closed_column_must_name_a_closed_question(tmp_path: Path) -> None:
    rows: Rows = {
        **GOOD,
        "Open questions": [
            ("o_reason", "Why?", "c_missing"),
            ("o_where", "Where?", "d_area"),
            ("o_why", "Why not?", "o_reason"),
        ],
    }
    with pytest.raises(DefinitionError) as raised:
        read_definition(write_workbook(tmp_path / "definition.xlsx", rows))
    problems = raised.value.problems
    assert len(problems) == 3
    assert all("related_closed_column" in problem for problem in problems)
    assert any("c_missing" in problem for problem in problems)
    assert any("d_area" in problem for problem in problems)
    assert any("o_reason" in problem for problem in problems)


def test_an_unknown_response_type_is_an_error(tmp_path: Path) -> None:
    rows: Rows = {
        **GOOD,
        "Closed questions": [
            ("c_route", "Support?", "single_select", "Support, Oppose"),
            ("c_size", "How big?", "number", "1, 2"),
        ],
    }
    with pytest.raises(DefinitionError) as raised:
        read_definition(write_workbook(tmp_path / "definition.xlsx", rows))
    problems = raised.value.problems
    # Two, not three: the open question that follows up c_route mustn't be
    # blamed for c_route's bad type as well.
    assert len(problems) == 2
    assert all("single-select, likert-5, multi-select" in problem for problem in problems)


def test_a_missing_sheet_or_header_is_an_error(tmp_path: Path) -> None:
    headers = {**SHEETS, "Closed questions": ("column_reference", "question_text", "response_type")}
    with pytest.raises(DefinitionError) as raised:
        read_definition(write_workbook(tmp_path / "definition.xlsx", GOOD, headers=headers))
    assert any("options" in problem for problem in raised.value.problems)

    only_two = {sheet: header for sheet, header in SHEETS.items() if sheet != "Open questions"}
    with pytest.raises(DefinitionError) as raised:
        read_definition(write_workbook(tmp_path / "definition.xlsx", GOOD, headers=only_two))
    assert any("Open questions" in problem for problem in raised.value.problems)


def test_a_duplicated_column_reference_is_an_error(tmp_path: Path) -> None:
    rows: Rows = {**GOOD, "Demographic questions": [("d_area", "Area?"), ("c_route", "Again?")]}
    with pytest.raises(DefinitionError) as raised:
        read_definition(write_workbook(tmp_path / "definition.xlsx", rows))
    assert any("c_route" in problem and "twice" in problem for problem in raised.value.problems)
