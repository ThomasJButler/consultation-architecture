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
on commas (consult.tokenise). Adjacent options that never appear apart are
the tell-tale of an option containing a comma that the workbook split
(docs/00), and each run of them gets one warning, naming the label it
spells, with the resolution to merge them.

The report carries column names, counts, row numbers and the distinct
values of demographic and closed columns. It never carries an open answer.
What the staging table can't hold at all (a header named row_no, a NUL, more
columns than a Postgres table takes) isn't reported but refused, as an
InputError with a code and a count, the way the reader refuses a hostile file.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from consult.cost import DEFAULT_RATES, Estimate, Rates, estimate
from consult.definition import ClosedQuestion, Definition, ResponseType, spelt_by
from consult.inputs import InputError, Refusal
from consult.responses import Responses, Row
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
# A header becomes a column name in the staging table. Postgres keeps the
# first NAMEDATALEN - 1 = 63 bytes of an identifier and drops the rest with
# a NOTICE the driver doesn't surface (PostgreSQL 17 manual, section 4.1.1,
# checked 26 September 2026), so a longer header would silently lose its
# column between COPY and ingest. It blocks here, before spend.
MAX_HEADER_BYTES = 63
# The staging table's own column (consult.stage), so no header may take it.
ROW_NO = "row_no"
# A Postgres table takes 1,600 columns and the staging table spends one on
# row_no (TooManyColumns at 1,601 on the local Postgres 16, 26 September
# 2026). Caps.max_columns defaults to the same, but it's a setting.
MAX_COLUMNS = 1_599
NUL = "\x00"
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
    estimate: Estimate


def suggested_role(header: str) -> Resolution:
    # "Response ID", "RespondentID" and "respondent_id" are the same header
    # to a person, so a space, a hyphen, a dot or a change of case all
    # separate words the way an underscore does.
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", header)
    words = [word for word in re.split(r"[^a-z0-9]+", spaced.lower()) if word]
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


def check_stageable_header(header: Sequence[str]) -> None:
    """Refuse a header the staging table can't hold, by a code and a count
    and never the name: more columns than the table takes, stage's own
    row_no, or a NUL, where libpq ends an identifier, so "notes<NUL>x" and
    "notes<NUL>y" would both name "notes". The count is the width, the
    column's position, or row 1. stage() runs the same check as its
    backstop."""
    if len(header) > MAX_COLUMNS:
        raise InputError(Refusal.TOO_MANY_COLUMNS, len(header))
    for position, name in enumerate(header, start=1):
        if name == ROW_NO:
            raise InputError(Refusal.RESERVED_HEADER, position)
        if NUL in name:
            raise InputError(Refusal.NUL_CHARACTER, 1)


def check_stageable_row(row: Row) -> None:
    """Refuse a NUL in a cell by the row's number: Postgres text can't store
    one, and psycopg refuses the row with a DataError at COPY."""
    if any(NUL in cell for cell in row.cells.values()):
        raise InputError(Refusal.NUL_CHARACTER, row.no)


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
    # Anything else is unknown until every row is counted, when
    # _claim_whole_labels takes back the whole labels of comma options.
    if cell in question.options:
        tally.values[cell] += 1
    else:
        tally.note_unknown(cell, row_no)


def _claim_whole_labels(tally: _Tally, options: tuple[str, ...]) -> None:
    """A single-select or likert cell is the chosen option written whole,
    so an option with a comma arrives as one value that a run of its split
    pieces spells (docs/00). Such a value is counted as that label, and
    _never_apart offers the merge, only when no piece was ever chosen
    alone: pieces chosen alone are real options, and a cell naming two of
    them is a stray answer that the default merge would fuse, blanking
    every answer to either (194 of c_route's 240 on the fixture with one
    "Support, Oppose" cell). A stray stays an unknown value."""
    for value in list(tally.unknown):
        run = spelt_by(value, options)
        if run is not None and not any(tally.values[piece] for piece in options[run]):
            tally.values[value] = tally.unknown.pop(value)
            del tally.unknown_rows[value]


def _never_apart(tally: _Tally, options: tuple[str, ...]) -> Iterable[Warning]:
    # Only a pair that sits next to each other in the option list can be two
    # pieces of one comma option (that's how split_options made them). A
    # label with two commas is three pieces and two such pairs, so each
    # maximal run of joined pairs is one option and gets one warning, named
    # by the label the run spells.
    joined = {
        pair
        for pair, together in tally.adjacent.items()
        if together and tally.values[pair[0]] == together == tally.values[pair[1]]
    }
    runs: list[list[str]] = []
    for index, option in enumerate(options):
        if index and (options[index - 1], option) in joined:
            runs[-1].append(option)
        else:
            runs.append([option])
    labels = [(", ".join(run), tally.values[run[0]]) for run in runs if len(run) > 1]
    # The whole labels a single-select or likert column counted
    # (_claim_whole_labels); a multi-select column counts pieces, all options.
    labels.extend(
        (value, count)
        for value, count in tally.values.items()
        if value not in options and spelt_by(value, options) is not None
    )
    for label, count in labels:
        yield Warning(
            WarningKind.OPTIONS_NEVER_APART,
            "",
            label,
            count,
            (),
            (Resolution.MERGE_OPTIONS,),
            Resolution.MERGE_OPTIONS,
        )


def validate(definition: Definition, responses: Responses, rates: Rates = DEFAULT_RATES) -> Report:
    check_stageable_header(responses.header)
    # Cells are keyed by header (consult.responses), so a repeated name
    # would lose a column without a trace and a blank one has no key at
    # all. Both block, and every column is described once, in file order.
    errors = [
        f"column {position} of the responses file has no name"
        for position, name in enumerate(responses.header, start=1)
        if not name
    ]
    errors.extend(
        f"header {name} appears twice in the responses file"
        for name, count in Counter(responses.header).items()
        if name and count > 1
    )
    errors.extend(
        f"header {name} is over {MAX_HEADER_BYTES} bytes and Postgres would cut it short"
        for name in dict.fromkeys(responses.header)
        if len(name.encode()) > MAX_HEADER_BYTES
    )
    header = tuple(name for name in dict.fromkeys(responses.header) if name)
    errors.extend(
        f"column {ref} is in the definition but not in the responses file"
        for ref in definition.column_refs
        if ref not in header
    )
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
    # Every column that could be the respondent id gets the check, since the
    # configure step hasn't picked one yet (docs/02, correction 8).
    id_columns = {ref for ref, role in roles.items() if role is Resolution.ROLE_RESPONDENT_ID}

    row_count = 0
    open_answers = 0
    for row in responses.rows():
        check_stageable_row(row)
        row_count += 1
        for ref, cell in row.cells.items():
            if ref not in tallies:
                continue
            tally = tallies[ref]
            if tally.kind is ColumnKind.UNMATCHED:
                if ref in id_columns and cell not in ("", NO_ANSWER):
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
        if ref in closed and closed[ref].response_type is not ResponseType.MULTI_SELECT:
            _claim_whole_labels(tally, closed[ref].options)
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
            # The rows name the duplicate; the value itself stays out, since
            # a column named like an id can hold email addresses.
            for _value, rows in sorted(tally.ids.items()):
                if len(rows) > 1:
                    warnings.append(
                        Warning(
                            WarningKind.DUPLICATE_RESPONDENT_ID,
                            ref,
                            None,
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
                    # The default has to be one that needs no choice from
                    # the reviewer, and it's what configure.defaults() and
                    # ingest apply when nobody chose.
                    Resolution.TREAT_AS_NOT_ANSWERED,
                )
            )
        options = closed[ref].options if ref in closed else ()
        for warning in _never_apart(tally, options):
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
    return Report(
        tuple(errors),
        tuple(warnings),
        tuple(columns),
        row_count,
        open_answers,
        estimate(open_answers, rates),
    )
