"""The validator: every check that can fail before spend, in one pass.

docs/02 section 3.2 is the table this implements, with its correction 8
(a repeated respondent id). Errors block; warnings carry a resolution the
configure step records per question; every demographic and closed column
lists its distinct values with counts so spelling and case variants
surface before anything is spent on the model. No row is ever dropped:
the validator describes the file, and the configure step decides.

"Warning" here is the design's word for a finding with a resolution, so
the class keeps it even though the standard library has one of its own.

What a cell means: `-` or blank is not answered for every kind of question;
`N/A` is a real value on a demographic or closed column (kept unless the
configure step says otherwise) and not answered on an open one. A
multi-select cell is tokenised against the option vocabulary, never split
on commas (consult.tokenise). Two options that never appear apart are the
tell-tale of an option containing a comma that the workbook split in two
(docs/00), and get their own warning with the resolution to merge them.

The report carries column names, counts, row numbers and the distinct
values of demographic and closed columns. It never carries an open answer.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from consult.definition import ClosedQuestion, Definition, ResponseType
from consult.responses import Responses
from consult.tokenise import tokenise

NO_ANSWER = "-"
NOT_APPLICABLE = "N/A"
EXAMPLE_ROWS = 5


class WarningKind(StrEnum):
    UNKNOWN_VALUE = "unknown_value"
    NOT_APPLICABLE = "not_applicable"
    OPTIONS_NEVER_APART = "options_never_apart"
    UNMATCHED_HEADER = "unmatched_header"
    DUPLICATE_RESPONDENT_ID = "duplicate_respondent_id"


class Resolution(StrEnum):
    MAP_TO_OPTION = "map_to_option"
    ADD_AS_OPTION = "add_as_option"
    TREAT_AS_NOT_ANSWERED = "treat_as_not_answered"
    KEEP_AS_VALUE = "keep_as_value"
    MERGE_OPTIONS = "merge_options"
    ROLE_RESPONDENT_ID = "role_respondent_id"
    ROLE_IDENTITY = "role_identity"
    ROLE_IGNORE = "role_ignore"
    IGNORE_COLUMN = "ignore_column"
    KEEP_FIRST_BLANK_REST = "keep_first_blank_rest"


class ColumnKind(StrEnum):
    DEMOGRAPHIC = "demographic"
    CLOSED = "closed"
    OPEN = "open"
    UNMATCHED = "unmatched"


UNKNOWN_VALUE_RESOLUTIONS = (
    Resolution.MAP_TO_OPTION,
    Resolution.ADD_AS_OPTION,
    Resolution.TREAT_AS_NOT_ANSWERED,
)
NOT_APPLICABLE_RESOLUTIONS = (Resolution.KEEP_AS_VALUE, Resolution.TREAT_AS_NOT_ANSWERED)
ROLE_RESOLUTIONS = (Resolution.ROLE_RESPONDENT_ID, Resolution.ROLE_IDENTITY, Resolution.ROLE_IGNORE)
DUPLICATE_ID_RESOLUTIONS = (Resolution.IGNORE_COLUMN, Resolution.KEEP_FIRST_BLANK_REST)

# Header words that say what an unmatched column is (docs/02, section 3.2:
# "the default for a column named like an id"). Identity first, so a
# column called "email_id" goes to the vault rather than becoming the key.
IDENTITY_WORDS = ("email", "mail", "name", "phone", "postcode", "address")
ID_WORDS = ("id", "ref", "reference", "respondent")


@dataclass(frozen=True)
class Warning:
    kind: WarningKind
    column_ref: str
    value: str | None
    count: int
    example_rows: tuple[int, ...]
    resolutions: tuple[Resolution, ...]
    default: Resolution


@dataclass(frozen=True)
class ColumnSummary:
    column_ref: str
    kind: ColumnKind
    answered: int
    not_answered: int
    values: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class Report:
    errors: tuple[str, ...]
    warnings: tuple[Warning, ...]
    columns: tuple[ColumnSummary, ...]
    row_count: int
    open_answer_count: int


def suggested_role(header: str) -> Resolution:
    words = header.lower().replace("-", "_").split("_")
    if any(word in IDENTITY_WORDS for word in words):
        return Resolution.ROLE_IDENTITY
    if any(word in ID_WORDS for word in words):
        return Resolution.ROLE_RESPONDENT_ID
    return Resolution.ROLE_IGNORE


@dataclass
class _Tally:
    """One column's running counts. Rows lists keep the first few row
    numbers per value as the examples the warning shows."""

    kind: ColumnKind
    answered: int = 0
    not_answered: int = 0
    values: Counter[str] = field(default_factory=Counter)
    unknown: Counter[str] = field(default_factory=Counter)
    unknown_rows: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    not_applicable_rows: list[int] = field(default_factory=list)
    adjacent: Counter[tuple[str, str]] = field(default_factory=Counter)
    ids: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))

    def note_unknown(self, value: str, row_no: int) -> None:
        self.unknown[value] += 1
        if len(self.unknown_rows[value]) < EXAMPLE_ROWS:
            self.unknown_rows[value].append(row_no)


def _sorted_values(counter: Counter[str]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(counter.items(), key=lambda pair: (-pair[1], pair[0])))


def _count_closed(tally: _Tally, question: ClosedQuestion, cell: str, row_no: int) -> None:
    if question.response_type is ResponseType.MULTI_SELECT:
        tokenised = tokenise(cell, question.options)
        tally.values.update(tokenised.tokens)
        for token in tokenised.unknown:
            tally.note_unknown(token, row_no)
        for first, second in zip(tokenised.tokens, tokenised.tokens[1:], strict=False):
            tally.adjacent[first, second] += 1
        return
    if cell in question.options:
        tally.values[cell] += 1
    else:
        tally.note_unknown(cell, row_no)


def _never_apart(tally: _Tally) -> Iterable[Warning]:
    for (first, second), together in sorted(tally.adjacent.items()):
        if together and tally.values[first] == together and tally.values[second] == together:
            yield Warning(
                WarningKind.OPTIONS_NEVER_APART,
                "",
                f"{first}, {second}",
                together,
                (),
                (Resolution.MERGE_OPTIONS,),
                Resolution.MERGE_OPTIONS,
            )


def validate(definition: Definition, responses: Responses) -> Report:
    header = responses.header
    errors = [
        f"column {ref} is in the definition but not in the responses file"
        for ref in definition.column_refs
        if ref not in header
    ]
    demographic = {q.column_ref for q in definition.demographic}
    closed = definition.closed_by_ref
    opened = {q.column_ref for q in definition.open}

    tallies: dict[str, _Tally] = {}
    roles: dict[str, Resolution] = {}
    for ref in header:
        if ref in demographic:
            tallies[ref] = _Tally(ColumnKind.DEMOGRAPHIC)
        elif ref in closed:
            tallies[ref] = _Tally(ColumnKind.CLOSED)
        elif ref in opened:
            tallies[ref] = _Tally(ColumnKind.OPEN)
        else:
            tallies[ref] = _Tally(ColumnKind.UNMATCHED)
            roles[ref] = suggested_role(ref)
    id_column = next(
        (ref for ref, role in roles.items() if role is Resolution.ROLE_RESPONDENT_ID), None
    )

    row_count = 0
    open_answers = 0
    for row in responses.rows():
        row_count += 1
        for ref, cell in row.cells.items():
            tally = tallies[ref]
            if tally.kind is ColumnKind.UNMATCHED:
                if ref == id_column and cell not in ("", NO_ANSWER):
                    tally.ids[cell].append(row.no)
                continue
            if cell in ("", NO_ANSWER) or (
                tally.kind is ColumnKind.OPEN and cell == NOT_APPLICABLE
            ):
                tally.not_answered += 1
                continue
            tally.answered += 1
            if tally.kind is ColumnKind.OPEN:
                open_answers += 1
            elif cell == NOT_APPLICABLE:
                tally.values[cell] += 1
                tally.not_applicable_rows.append(row.no)
            elif tally.kind is ColumnKind.DEMOGRAPHIC:
                tally.values[cell] += 1
            else:
                _count_closed(tally, closed[ref], cell, row.no)

    warnings: list[Warning] = []
    columns: list[ColumnSummary] = []
    for ref in header:
        tally = tallies[ref]
        columns.append(
            ColumnSummary(
                ref, tally.kind, tally.answered, tally.not_answered, _sorted_values(tally.values)
            )
        )
        if tally.kind is ColumnKind.UNMATCHED:
            warnings.append(
                Warning(
                    WarningKind.UNMATCHED_HEADER, ref, None, 0, (), ROLE_RESOLUTIONS, roles[ref]
                )
            )
            for value, rows in sorted(tally.ids.items()):
                if len(rows) > 1:
                    warnings.append(
                        Warning(
                            WarningKind.DUPLICATE_RESPONDENT_ID,
                            ref,
                            value,
                            len(rows),
                            tuple(rows[:EXAMPLE_ROWS]),
                            DUPLICATE_ID_RESOLUTIONS,
                            Resolution.KEEP_FIRST_BLANK_REST,
                        )
                    )
            continue
        if tally.not_applicable_rows:
            warnings.append(
                Warning(
                    WarningKind.NOT_APPLICABLE,
                    ref,
                    NOT_APPLICABLE,
                    len(tally.not_applicable_rows),
                    tuple(tally.not_applicable_rows[:EXAMPLE_ROWS]),
                    NOT_APPLICABLE_RESOLUTIONS,
                    Resolution.KEEP_AS_VALUE,
                )
            )
        for value, count in _sorted_values(tally.unknown):
            warnings.append(
                Warning(
                    WarningKind.UNKNOWN_VALUE,
                    ref,
                    value,
                    count,
                    tuple(tally.unknown_rows[value]),
                    UNKNOWN_VALUE_RESOLUTIONS,
                    Resolution.MAP_TO_OPTION,
                )
            )
        for warning in _never_apart(tally):
            warnings.append(
                Warning(
                    warning.kind,
                    ref,
                    warning.value,
                    warning.count,
                    warning.example_rows,
                    warning.resolutions,
                    warning.default,
                )
            )

    order = {kind: index for index, kind in enumerate(WarningKind)}
    warnings.sort(key=lambda w: (order[w.kind], w.column_ref, -w.count, w.value or ""))
    return Report(tuple(errors), tuple(warnings), tuple(columns), row_count, open_answers)
