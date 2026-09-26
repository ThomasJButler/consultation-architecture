"""The definition workbook, read into typed questions.

The format is the one docs/00 describes: three sheets with fixed headers,
`options` joined by commas, `related_closed_column` naming a closed
question or holding `-` or nothing, and a `response_type` from the fixed
vocabulary of three that docs/02 section 3.2 names. The workbook is an
importer that pre-fills the configure step; it isn't the configuration
(docs/02, step 3), which is why an option containing a comma comes out of
here as two options and it's the validator's job to notice.

Every problem is collected and raised together, so the configure screen
can list them all rather than one per upload.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from openpyxl.workbook.workbook import Workbook

from consult.inputs import DEFAULT_CAPS, Caps, check_cell, check_width, guarded, open_workbook

SHEETS: Final[dict[str, tuple[str, ...]]] = {
    "Demographic questions": ("column_reference", "question_text"),
    "Closed questions": ("column_reference", "question_text", "response_type", "options"),
    "Open questions": ("column_reference", "question_text", "related_closed_column"),
}
NO_VALUE: Final = "-"


class ResponseType(StrEnum):
    SINGLE_SELECT = "single-select"
    LIKERT_5 = "likert-5"
    MULTI_SELECT = "multi-select"


RESPONSE_TYPES = ", ".join(kind.value for kind in ResponseType)


@dataclass(frozen=True)
class DemographicQuestion:
    column_ref: str
    text: str


@dataclass(frozen=True)
class ClosedQuestion:
    column_ref: str
    text: str
    response_type: ResponseType
    options: tuple[str, ...]


@dataclass(frozen=True)
class OpenQuestion:
    column_ref: str
    text: str
    related_closed_column: str | None


@dataclass(frozen=True)
class Definition:
    demographic: tuple[DemographicQuestion, ...]
    closed: tuple[ClosedQuestion, ...]
    open: tuple[OpenQuestion, ...]

    @property
    def column_refs(self) -> tuple[str, ...]:
        return (
            *(q.column_ref for q in self.demographic),
            *(q.column_ref for q in self.closed),
            *(q.column_ref for q in self.open),
        )

    @property
    def closed_by_ref(self) -> dict[str, ClosedQuestion]:
        return {q.column_ref: q for q in self.closed}


class DefinitionError(Exception):
    """Every problem found in the workbook, together."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


def split_options(cell: str) -> tuple[str, ...]:
    """Options as the workbook joins them: on the comma, trimmed, empties dropped."""
    return tuple(option.strip() for option in cell.split(",") if option.strip())


def spelt_by(label: str, options: Sequence[str]) -> slice | None:
    """Where in `options` a run of two or more adjacent options, joined by
    ", ", spells `label`: the pieces split_options made of it, or None."""
    for start in range(len(options)):
        joined = options[start]
        for end in range(start + 1, len(options)):
            joined = f"{joined}, {options[end]}"
            if joined == label:
                return slice(start, end + 1)
            if not label.startswith(joined):
                break
    return None


def _cell(value: object) -> str:
    return "" if value is None else str(value).strip()


def _rows(
    workbook: Workbook, sheet: str, problems: list[str], caps: Caps
) -> Iterator[dict[str, str]]:
    """Each data row of a sheet as a dict by header, after checking the header.

    The same cell and width caps as the responses file: the workbook is
    smaller and the policy team's own, but it's still an upload.
    """
    if sheet not in workbook.sheetnames:
        problems.append(f"{sheet}: sheet missing")
        return
    worksheet = workbook[sheet]
    # Rows by what the sheet holds, not by the dimension it declares, as in
    # consult.responses.
    worksheet.reset_dimensions()
    rows = (
        check_width(tuple(check_cell(_cell(value), caps) for value in row), caps)
        for row in guarded(worksheet.iter_rows(values_only=True))
    )
    header = next(rows, ())
    expected = SHEETS[sheet]
    if header[: len(expected)] != expected:
        problems.append(f"{sheet}: headers must be {', '.join(expected)}")
        return
    for values in rows:
        if not any(values):
            continue
        yield dict(zip(expected, values, strict=False))


def read_definition(path: Path, caps: Caps = DEFAULT_CAPS) -> Definition:
    problems: list[str] = []
    with open_workbook(path, caps) as workbook:
        demographic = tuple(
            DemographicQuestion(row["column_reference"], row["question_text"])
            for row in _rows(workbook, "Demographic questions", problems, caps)
        )
        closed: list[ClosedQuestion] = []
        # Every closed column reference, bad response type or not, so one
        # problem doesn't cascade into a second on the open question that
        # follows it up.
        closed_refs: set[str] = set()
        for row in _rows(workbook, "Closed questions", problems, caps):
            closed_refs.add(row["column_reference"])
            try:
                kind = ResponseType(row["response_type"])
            except ValueError:
                problems.append(
                    f"Closed questions: {row['column_reference']} has response_type "
                    f"{row['response_type']!r}, not one of {RESPONSE_TYPES}"
                )
                continue
            closed.append(
                ClosedQuestion(
                    row["column_reference"],
                    row["question_text"],
                    kind,
                    split_options(row["options"]),
                )
            )
        opened: list[OpenQuestion] = []
        for row in _rows(workbook, "Open questions", problems, caps):
            related = row["related_closed_column"]
            opened.append(
                OpenQuestion(
                    row["column_reference"],
                    row["question_text"],
                    None if related in ("", NO_VALUE) else related,
                )
            )

    definition = Definition(demographic, tuple(closed), tuple(opened))
    seen: set[str] = set()
    for ref in definition.column_refs:
        if ref in seen:
            problems.append(f"column_reference {ref} appears twice")
        seen.add(ref)
    for question in definition.open:
        related_ref = question.related_closed_column
        if related_ref is not None and related_ref not in closed_refs:
            problems.append(
                f"Open questions: {question.column_ref} has related_closed_column "
                f"{related_ref!r}, which is not a closed question"
            )
    if problems:
        raise DefinitionError(problems)
    return definition
