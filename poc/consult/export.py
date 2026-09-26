"""docs/02 step 12's XLSX export.

Written write-only, cell by cell, as text: `neutralise` and a `data_type`
forced to "s" are two locks on one door against a formula reading live the
moment a department opens the file (docs/06, section 2.7; THREAT_MODEL.md,
row 7). The file's own no-answer marker (docs/00) is a lone "-"; this
module keeps using it, so a reviewer who has read the upload sees the same
mark mean the same thing in the file they get back.

The Responses sheet carries an identity column docs/04 keeps in its own
schema. docs/06 section 4 names why the export command alone may read it:
putting an identity column back into the department's own spreadsheet is
the one thing that schema's read grant is for, so `write_workbook` reads
under `store.EXPORT_ROLE` rather than the login user's own privileges.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from uuid import UUID

import psycopg
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import Cell
from openpyxl.utils.exceptions import IllegalCharacterError
from openpyxl.worksheet._write_only import WriteOnlyWorksheet
from psycopg.pq import TransactionStatus
from psycopg.rows import DictRow

from consult import query
from consult.query import Filter
from consult.store import EXPORT_ROLE, as_role, identity_columns

# docs/06, section 2.7: a value starting with one of these opens as a live
# formula in Excel or LibreOffice the moment the file is opened
# (THREAT_MODEL.md, row 7).
_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
_PREFIX = "'"
NO_ANSWER = "-"
# docs/04 section 6's export query has no OFFSET; this is the page size
# the cursor (the last id seen) is read in, looped until a page comes
# back shorter than this, which stops the read without one further
# round trip for an empty final page.
_KEYSET_PAGE = 1000

# openpyxl's own title setter (openpyxl.workbook.child.INVALID_TITLE_REGEX)
# refuses any of these six in a sheet title, and Excel's own sheet-title
# limit is 31 characters, measured against openpyxl 3.1.5, 26 September
# 2026.
_INVALID_SHEET_CHARS = re.compile(r"[\\*?:/\[\]]")
_MAX_SHEET_TITLE = 31

# XML 1.0's Char production (section 2.2) admits tab, LF, CR, U+0020 to
# U+D7FF, U+E000 to U+FFFD and U+10000 up, and this is the rest below
# U+10000: the C0 controls openpyxl's own ILLEGAL_CHARACTERS_RE names
# (openpyxl 3.1.5, cell.py line 45), the surrogates, and U+FFFE and
# U+FFFF, which check_string lets through and ElementTree, openpyxl's
# writer here without lxml, puts raw into a sheet part no XML parser
# will read.
_XML_FORBIDDEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")

# ECMA-376 Part 1, 22.9.2.19 (ST_Xstring): a spreadsheet decodes _xHHHH_
# in a cell's text as the character with that code, and openpyxl 3.1.5
# writes the text as given, so the underscore that opens one is written
# as _x005F_, the underscore's own escape, the form XlsxWriter writes.
# A lookahead rather than a match, so an underscore that closes one
# sequence and opens the next is escaped too.
_OOXML_ESCAPE = re.compile("_(?=x[0-9A-Fa-f]{4}_)")
_ESCAPED_UNDERSCORE = "_x005F_"

# Excel's own cell limit, and the length openpyxl 3.1.5's check_string
# cuts every string to without a word (cell.py line 163). The input's
# cell cap is the same number (CONSULT_MAX_CELL_CHARS), so the prefix or
# an escape can take a value at the cap past it. A longer value is cut
# here instead, short enough to end in the mark, and counted.
_MAX_CELL_CHARS = 32767
_CUT_MARK = "..."


class ExportError(Exception):
    """An export refused, carrying a code and nothing else.

    `CELL_VALUE_ILLEGAL` is `_cell`'s backstop. Escaping every
    `_XML_FORBIDDEN` match before the cell is built should leave
    openpyxl's own `check_string` nothing left to refuse; if some
    character it still refuses reaches this anyway,
    `IllegalCharacterError` puts the whole value in its message (openpyxl
    3.1.5, cell.py lines 164-165), and THREAT_MODEL.md section 2, line 2
    forbids an open answer or a vault value at any level, an exception
    message included, so this carries nothing of the value it failed on.

    `CONNECTION_BUSY` is `write_workbook`'s refusal of a connection with
    a transaction open, which it has no business ending.
    """

    CELL_VALUE_ILLEGAL = "cell_value_illegal"
    CONNECTION_BUSY = "connection_busy"

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _sheet_title(column_ref: str, used: set[str]) -> str:
    """A per-question summary sheet's title, valid wherever `column_ref`
    isn't: `column_ref` is a free-text header from the definition
    workbook (docs/00), not a sheet-safe string, so the six characters
    `_INVALID_SHEET_CHARS` names are stripped and the result truncated to
    Excel's limit before openpyxl ever sees it. Excel also refuses a
    title that starts or ends with an apostrophe, so those are stripped
    from both ends.

    `used` is mutated and holds titles casefolded: titles are
    de-duplicated here, with a numbered suffix, rather than left to
    openpyxl's own `avoid_duplicate_name`, which runs after the character
    check above has already raised, and compared without case, as Excel
    and `avoid_duplicate_name` both compare them. Two titles differing
    only in case would otherwise both pass here and have openpyxl append
    a digit to the second, past the limit.
    """
    base = _INVALID_SHEET_CHARS.sub("", column_ref).lstrip("'")
    title = f"{base} summary"[:_MAX_SHEET_TITLE].rstrip("'")
    if title.casefold() not in used:
        used.add(title.casefold())
        return title
    n = 2
    while True:
        suffix = f" ({n})"
        candidate = title[: _MAX_SHEET_TITLE - len(suffix)] + suffix
        if candidate.casefold() not in used:
            used.add(candidate.casefold())
            return candidate
        n += 1


def neutralise(value: str) -> str:
    """A value starting with a formula trigger gets a leading apostrophe,
    the convention CSV and a cell typed by hand both read as "force this
    cell to text" (docs/06, section 2.7). In the XLSX `write_workbook`
    saves, the apostrophe is written as part of the string and stays
    visible when the file is opened: openpyxl sets no `quotePrefix` on
    the cell (measured against openpyxl 3.1.5, 26 September 2026), so
    what actually stops the value evaluating there is `_cell`'s own
    `data_type` forced to "s", not the apostrophe. The file's own
    no-answer marker, a lone "-", is left alone: it isn't a formula
    wherever it's read back, and prefixing it would change what "not
    answered" looks like (docs/00).
    """
    if value == NO_ANSWER:
        return value
    if value.startswith(_TRIGGERS):
        return _PREFIX + value
    return value


def _or_dash(value: object) -> object:
    """docs/00's no-answer marker for a value this schema has none of,
    rather than a blank cell a reader can't tell from one that just wasn't
    read yet."""
    return NO_ANSWER if value is None else value


def _cell(ws: WriteOnlyWorksheet, value: object) -> tuple[Cell, bool]:
    """Every cell as text, twice over (docs/06, section 2.7): `neutralise`
    above, and `data_type` forced to "s" on top of it, since openpyxl
    infers "f" from a leading "=" on a plain string otherwise (measured
    against openpyxl 3.1.5, 26 September 2026).

    A C0 control character other than tab, newline or carriage return
    fails openpyxl's own `check_string` with the raw value in the
    exception message it raises (cell.py lines 164-165), and U+FFFE or
    U+FFFF passes it and leaves the sheet part malformed XML. An open
    answer or a vault identity value can carry either, and THREAT_MODEL.md
    section 2, line 2 forbids a value riding an exception at any level,
    so every character XML 1.0 forbids (`_XML_FORBIDDEN`) is escaped to
    the visible form `report.shown` already uses for a control character
    before openpyxl ever sees the string. The `except` is a backstop for
    a character neither list anticipated: it still can't let the value
    through.

    An `_xHHHH_` sequence in the text is escaped after `neutralise`
    (`_OOXML_ESCAPE`), so Excel shows it as typed rather than decoding
    `_x003D_` into an `=` that `neutralise` never saw.

    Measured last, once the prefix and the escapes are in: a text past
    `_MAX_CELL_CHARS` is cut to end in `_CUT_MARK`, and the second value
    returned says so.
    """
    text = "" if value is None else str(value)
    text = _XML_FORBIDDEN.sub(lambda match: repr(match.group())[1:-1], text)
    text = _OOXML_ESCAPE.sub(_ESCAPED_UNDERSCORE, neutralise(text))
    cut = len(text) > _MAX_CELL_CHARS
    if cut:
        text = text[: _MAX_CELL_CHARS - len(_CUT_MARK)] + _CUT_MARK
    try:
        cell = WriteOnlyCell(ws, value=text)
    except IllegalCharacterError:
        raise ExportError(ExportError.CELL_VALUE_ILLEGAL) from None
    cell.data_type = "s"
    return cell, cut


def _row(ws: WriteOnlyWorksheet, values: list[object]) -> int:
    """One row of text cells; returns how many of them were cut."""
    cells = [_cell(ws, value) for value in values]
    ws.append([built for built, _cut in cells])
    return sum(cut for _built, cut in cells)


@dataclass(frozen=True)
class _Question:
    id: UUID
    column_ref: str
    kind: str


def _questions(conn: psycopg.Connection[DictRow], consultation_id: UUID) -> list[_Question]:
    """Every question the Responses sheet carries a column for, in the
    order docs/02 step 12 describes them: identity, then demographic and
    closed together, then open (docs/04 section 5's own bucketing), and by
    ordinal inside each so the shape matches the file's own column order
    on this fixture."""
    rows = conn.execute(
        """
        SELECT id, column_ref, kind FROM question
         WHERE consultation_id = %s AND kind IN ('identity', 'demographic', 'closed', 'open')
         ORDER BY CASE kind
                    WHEN 'identity' THEN 0
                    WHEN 'demographic' THEN 1
                    WHEN 'closed' THEN 1
                    ELSE 2
                  END, ordinal
        """,
        (consultation_id,),
    ).fetchall()
    return [_Question(row["id"], row["column_ref"], row["kind"]) for row in rows]


def _respondent_id_header(conn: psycopg.Connection[DictRow], consultation_id: UUID) -> str | None:
    """The header the file's own respondent-id column had (docs/04,
    section 1): the id column has no question row of its own, so it comes
    from `column_roles`, where `configure.configure` records it."""
    row = conn.execute(
        "SELECT column_roles ->> 'respondent_id' AS header FROM consultation WHERE id = %s",
        (consultation_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"consultation {consultation_id} does not exist")
    header = row["header"]
    return str(header) if header is not None else None


@dataclass(frozen=True)
class _Respondent:
    id: int
    external_id: str | None
    attrs: dict[str, list[str]]


def _respondents(conn: psycopg.Connection[DictRow], consultation_id: UUID) -> list[_Respondent]:
    """The workbook's rows: every respondent, canonical and duplicate
    alike, since the file the department gets back is this consultation's
    whole answer set. One of the reads `write_workbook` makes inside its
    role block, and the one test_export.py's
    test_export_reads_as_the_export_role wraps to read `current_user` from
    inside that block."""
    rows = conn.execute(
        "SELECT id, external_id, attrs FROM respondent WHERE consultation_id = %s ORDER BY id",
        (consultation_id,),
    ).fetchall()
    return [_Respondent(row["id"], row["external_id"], row["attrs"]) for row in rows]


def _identity(
    conn: psycopg.Connection[DictRow], consultation_id: UUID
) -> dict[int, dict[str, str]]:
    """The vault's own values, by respondent then column (docs/06, section
    4). `store.identity_columns` is the one place that names the schema on
    this path (test_repo_rules.py)."""
    identity: dict[int, dict[str, str]] = {}
    for row in identity_columns(conn, consultation_id):
        identity.setdefault(row["respondent_id"], {})[row["column_ref"]] = row["value_text"]
    return identity


def _open_answers(
    conn: psycopg.Connection[DictRow], question_id: UUID
) -> dict[int, tuple[int | None, str | None]]:
    """docs/04 section 6's export keyset query, looped: no OFFSET, so the
    cursor is the last id seen and `answer_question_id_id` gives every
    page an index scan rather than a re-sort. Stops on a short page
    rather than reading on for an empty one."""
    by_respondent: dict[int, tuple[int | None, str | None]] = {}
    after = 0
    while True:
        rows = conn.execute(
            "SELECT id, respondent_id, value_text FROM answer"
            " WHERE question_id = %s AND id > %s ORDER BY id LIMIT %s",
            (question_id, after, _KEYSET_PAGE),
        ).fetchall()
        if not rows:
            return by_respondent
        for row in rows:
            by_respondent[row["respondent_id"]] = (row["id"], row["value_text"])
        after = rows[-1]["id"]
        if len(rows) < _KEYSET_PAGE:
            return by_respondent


@dataclass(frozen=True)
class _ThemeSet:
    version_id: UUID | None
    version_no: int | None
    keys: tuple[str, ...]


def _latest_signed_off(conn: psycopg.Connection[DictRow], question_id: UUID) -> _ThemeSet:
    # A reopen leaves the earlier signed-off version in place beside the
    # new one (transitions.reopen_for_correction), so the version that
    # counts is the highest version_no, the same reasoning query._latest_signed_off
    # composes into SQL; this one runs it directly since export.py has no
    # scope CTE to fold it into.
    version = conn.execute(
        "SELECT id, version_no FROM theme_set_version"
        " WHERE question_id = %s AND status = 'signed_off'"
        " ORDER BY version_no DESC LIMIT 1",
        (question_id,),
    ).fetchone()
    if version is None:
        return _ThemeSet(None, None, ())
    # The shortlist and its fallbacks only: sign_off copies the longlist
    # in too, for lineage, but mapping never offers the model a longlist
    # key (mapping._shortlist), so a column for one could never be marked.
    keys = conn.execute(
        "SELECT key FROM theme WHERE theme_set_version_id = %s AND NOT is_longlist ORDER BY key",
        (version["id"],),
    ).fetchall()
    return _ThemeSet(version["id"], version["version_no"], tuple(row["key"] for row in keys))


def _tags(conn: psycopg.Connection[DictRow], version_id: UUID | None) -> dict[int, set[str]]:
    """Live tags only (`retracted_at IS NULL`), by answer id: a retraction
    is a human correction and the export shouldn't show a theme nobody
    stands behind any more (docs/02, step 11)."""
    if version_id is None:
        return {}
    rows = conn.execute(
        "SELECT at.answer_id, th.key FROM answer_theme at"
        " JOIN theme th ON th.id = at.theme_id"
        " WHERE at.theme_set_version_id = %s AND at.retracted_at IS NULL",
        (version_id,),
    ).fetchall()
    tags: dict[int, set[str]] = {}
    for row in rows:
        tags.setdefault(row["answer_id"], set()).add(row["key"])
    return tags


@dataclass(frozen=True)
class _ManifestQuestion:
    column_ref: str
    version_id: UUID | None
    version_no: int | None
    find_themes_model_alias: str | None
    map_themes_model_alias: str | None
    tag_count: int
    unprocessable_count: int


def _manifest_question(
    conn: psycopg.Connection[DictRow], question: _Question, theme_set: _ThemeSet, tag_count: int
) -> _ManifestQuestion:
    aliases = {
        row["kind"]: row["model_alias"]
        for row in conn.execute(
            "SELECT DISTINCT ON (kind) kind, model_alias FROM job"
            " WHERE question_id = %s AND kind IN ('find_themes', 'map_themes')"
            " ORDER BY kind, created_at DESC",
            (question.id,),
        ).fetchall()
    }
    # Scoped to the version's own map_themes job, the same one `aliases`
    # above already picks by created_at: a reopen's new run maps every
    # answer again under a new job (docs/02, step 9), so counting every
    # map_themes job the question has ever had double-counts an answer
    # refused in both. Distinct answer ids, not batches: a retried batch
    # that eventually succeeds leaves its earlier 'done' row in place
    # beside the later one (ADR-002's checkpoint), so a plain count(*)
    # over statuses without unnest would over-count that answer too.
    unprocessable = conn.execute(
        """
        SELECT count(DISTINCT ua.answer_id) AS n
          FROM job_batch jb, unnest(jb.answer_ids) AS ua (answer_id)
         WHERE jb.status = 'unprocessable'
           AND jb.job_id = (SELECT id FROM job
                              WHERE question_id = %s AND kind = 'map_themes'
                              ORDER BY created_at DESC LIMIT 1)
        """,
        (question.id,),
    ).fetchone()
    return _ManifestQuestion(
        column_ref=question.column_ref,
        version_id=theme_set.version_id,
        version_no=theme_set.version_no,
        find_themes_model_alias=aliases.get("find_themes"),
        map_themes_model_alias=aliases.get("map_themes"),
        tag_count=tag_count,
        unprocessable_count=int(unprocessable["n"]) if unprocessable is not None else 0,
    )


def _duplicate_counts(conn: psycopg.Connection[DictRow], consultation_id: UUID) -> tuple[int, int]:
    row = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM answer WHERE consultation_id = %(id)s
             AND duplicate_of_answer_id IS NOT NULL) AS answers,
          (SELECT count(*) FROM respondent WHERE consultation_id = %(id)s
             AND duplicate_of IS NOT NULL) AS respondents
        """,
        {"id": consultation_id},
    ).fetchone()
    if row is None:
        # Both subselects always return exactly one row each, so the outer
        # query returning none means Postgres has broken more than this.
        raise LookupError("duplicate counts query returned no row")
    return int(row["answers"]), int(row["respondents"])


def _department_id(conn: psycopg.Connection[DictRow], consultation_id: UUID) -> UUID:
    """The consultation's own department. `query.theme_table` takes
    `department_id` as a required keyword now that `scope` scopes every
    query to the caller's department (query.py, "Scope every query to the
    caller's department"); the export reads the consultation's own row
    for it, since the export role has no separate caller identity to
    scope by yet (open work, plans/PR-09-poc-query-export-cli.md)."""
    row = conn.execute(
        "SELECT department_id FROM consultation WHERE id = %s", (consultation_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"consultation {consultation_id} does not exist")
    return UUID(str(row["department_id"]))


def _snapshot_now(conn: psycopg.Connection[DictRow]) -> datetime:
    """The exporting transaction's own `now()`, fixed at the instant the
    REPEATABLE READ snapshot was taken (PostgreSQL 17 manual, 13.2.2):
    the instant the rows around it describe, not the instant Python
    happens to reach this line in the calling process."""
    row = conn.execute("SELECT now() AS now").fetchone()
    if row is None:
        # now() with no FROM always returns one row; a database that
        # answers otherwise has broken more than this query.
        raise LookupError("now() returned no row")
    value: datetime = row["now"]
    return value


def _consultation_summary(
    conn: psycopg.Connection[DictRow], consultation_id: UUID
) -> tuple[str, UUID, date | None]:
    row = conn.execute(
        "SELECT name, run_id, retention_until FROM consultation WHERE id = %s",
        (consultation_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"consultation {consultation_id} does not exist")
    return str(row["name"]), UUID(str(row["run_id"])), row["retention_until"]


@dataclass(frozen=True)
class _Manifest:
    consultation_id: UUID
    consultation_name: str
    run_id: UUID
    retention_until: date | None
    duplicate_answers: int
    duplicate_respondents: int
    questions: tuple[_ManifestQuestion, ...]
    exported_at: datetime


def _write_responses(
    ws: WriteOnlyWorksheet,
    questions: list[_Question],
    respondent_id_header: str | None,
    respondents: list[_Respondent],
    identity: dict[int, dict[str, str]],
    open_questions: list[_Question],
    answers: dict[UUID, dict[int, tuple[int | None, str | None]]],
    theme_sets: dict[UUID, _ThemeSet],
    tags: dict[UUID, dict[int, set[str]]],
) -> int:
    """docs/02 step 12's first sheet: the respondent id, the vault's
    identity column, every demographic and closed answer from
    `respondent.attrs`, every open question's text, then one column per
    theme per open question (docs/04, section 6). Returns the count of
    cells cut at the cap."""
    other_questions = [question for question in questions if question.kind != "open"]

    header: list[object] = []
    if respondent_id_header is not None:
        header.append(respondent_id_header)
    header.extend(question.column_ref for question in other_questions)
    header.extend(question.column_ref for question in open_questions)
    for question in open_questions:
        header.extend(f"{question.column_ref}: {key}" for key in theme_sets[question.id].keys)
    cut = _row(ws, header)

    for respondent in respondents:
        values: list[object] = []
        if respondent_id_header is not None:
            values.append(_or_dash(respondent.external_id))
        for question in other_questions:
            if question.kind == "identity":
                found = identity.get(respondent.id, {}).get(question.column_ref)
                values.append(_or_dash(found))
            else:
                cells = respondent.attrs.get(question.column_ref)
                values.append(", ".join(cells) if cells else NO_ANSWER)

        # One lookup per open question, read once and used twice below, so
        # the text columns and the theme columns agree on which answer
        # they're describing.
        current = {
            question.id: answers[question.id].get(respondent.id, (None, None))
            for question in open_questions
        }
        for question in open_questions:
            _answer_id, text = current[question.id]
            values.append(_or_dash(text))
        for question in open_questions:
            answer_id, _text = current[question.id]
            marked = tags[question.id].get(answer_id, set()) if answer_id is not None else set()
            values.extend("1" if key in marked else None for key in theme_sets[question.id].keys)
        cut += _row(ws, values)
    return cut


def _write_summary(ws: WriteOnlyWorksheet, table: query.ThemeTable, hidden: int) -> int:
    """docs/02 step 12's per-question summary: key, label, respondents and
    the denominator, straight from `query.theme_table` under the default
    filter, so this number and the per-question dashboard's can't drift
    apart. The default filter hides duplicates (docs/02 section 7,
    decision 9) and the Responses sheet marks every row, so a line under
    the table, a blank row apart so it isn't read as a theme, names the
    `hidden` count. Returns the count of cells cut at the cap."""
    cut = _row(ws, ["key", "label", "respondents", "denominator"])
    for row in table.rows:
        cut += _row(ws, [row.key, row.label, row.respondents, table.denominator])
    ws.append([])
    cut += _row(ws, [f"Duplicate answers hidden: {hidden} (the Responses sheet marks every row)"])
    return cut


def _write_manifest(ws: WriteOnlyWorksheet, manifest: _Manifest, cut: int) -> None:
    """docs/02 step 12's manifest, cut to what this export computes. No
    prompt hash: `job.prompt_sha256` is a real column (docs/04 section 1)
    that nothing here, or anywhere else in this codebase, writes yet. No
    agreement rate either, and that one has no column to write at all:
    it would have to be derived from the tags, and nothing computes it.

    `cut` is the other sheets' count of cells cut at the cap, and
    `truncated_cells` adds the rows above it on this one. The rows below
    it hold ids, counts, model aliases and column refs, each far short
    of the cap: a column ref is a header stage.py holds to 63 bytes.
    """
    cut += _row(ws, ["field", "value"])
    cut += _row(ws, ["consultation_id", manifest.consultation_id])
    cut += _row(ws, ["consultation_name", manifest.consultation_name])
    cut += _row(ws, ["run_id", manifest.run_id])
    cut += _row(ws, ["retention_until", _or_dash(manifest.retention_until)])
    cut += _row(ws, ["exported_at", manifest.exported_at.isoformat()])
    cut += _row(ws, ["duplicate_answers", manifest.duplicate_answers])
    cut += _row(ws, ["duplicate_respondents", manifest.duplicate_respondents])
    _row(ws, ["truncated_cells", cut])
    _row(
        ws,
        [
            "question",
            "theme_set_version_id",
            "theme_set_version_no",
            "find_themes_model_alias",
            "map_themes_model_alias",
            "tag_count",
            "unprocessable_count",
        ],
    )
    for question in manifest.questions:
        _row(
            ws,
            [
                question.column_ref,
                _or_dash(question.version_id),
                _or_dash(question.version_no),
                _or_dash(question.find_themes_model_alias),
                _or_dash(question.map_themes_model_alias),
                question.tag_count,
                question.unprocessable_count,
            ],
        )


def _in_transaction(conn: psycopg.Connection[DictRow]) -> bool:
    """Anything but idle: a statement running, a transaction open or
    failed, or the connection lost. A call rather than the comparison
    inline, because the status changes under `write_workbook`'s own
    statements and mypy would otherwise carry the first comparison's
    answer into its `finally` and call the rollback unreachable."""
    return conn.info.transaction_status != TransactionStatus.IDLE


@dataclass(frozen=True)
class Exported:
    respondents: int
    answers: int
    tags: int
    sheets: int
    truncated_cells: int


def write_workbook(
    conn: psycopg.Connection[DictRow], consultation_id: UUID, path: Path
) -> Exported:
    """docs/02 step 12's XLSX: the Responses sheet, one summary sheet per
    open question and a manifest, saved to `path`.

    The whole gather runs inside one REPEATABLE READ, read-only
    transaction: Postgres fixes the snapshot at that transaction's first
    statement and holds it for every statement after (PostgreSQL 17
    manual, 13.2.2), so a retraction, a human tag, a worker insert or a
    new sign-off committed by another session mid-export can't split the
    workbook across two states of the database. The summary sheets aren't
    the Responses sheet's totals: they count respondents under the default
    filter, duplicates hidden (docs/02 section 7, decision 9), where the
    Responses sheet marks every row, and each says under its table how
    many answers that hides. psycopg only applies `isolation_level` and
    `read_only` to the next transaction, and only while the connection is
    idle, so a connection with a transaction open is refused
    (`ExportError.CONNECTION_BUSY`) rather than committed: those writes
    are the caller's to commit or roll back. Both settings are saved
    before they're set and put back in `finally`, so `conn` comes back to
    its caller at its own isolation level, not this function's.
    `exported_at` is read back as that transaction's own `now()`
    (`_snapshot_now`) rather than Python's clock, for the same reason: it
    names the instant the snapshot was taken.

    The reads run as `store.EXPORT_ROLE` (docs/06, section 2.4 as
    corrected), the one role with a grant on the vault and none on the
    pipeline's writes.
    """
    if _in_transaction(conn):
        raise ExportError(ExportError.CONNECTION_BUSY)
    isolation_level, read_only = conn.isolation_level, conn.read_only
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    conn.read_only = True
    try:
        with as_role(conn, EXPORT_ROLE):
            questions = _questions(conn, consultation_id)
            respondent_id_header = _respondent_id_header(conn, consultation_id)
            respondents = _respondents(conn, consultation_id)
            identity = _identity(conn, consultation_id)
            open_questions = [question for question in questions if question.kind == "open"]

            answers = {question.id: _open_answers(conn, question.id) for question in open_questions}
            theme_sets = {
                question.id: _latest_signed_off(conn, question.id) for question in open_questions
            }
            tags = {
                question.id: _tags(conn, theme_sets[question.id].version_id)
                for question in open_questions
            }
            tag_counts = {
                question.id: sum(len(keys) for keys in tags[question.id].values())
                for question in open_questions
            }
            department_id = _department_id(conn, consultation_id)
            summaries = {
                question.id: query.theme_table(
                    conn, question.id, Filter(), department_id=department_id
                )
                for question in open_questions
            }
            # The same scope with duplicates shown: what its denominator
            # adds to the default one is what the summary sheet hides.
            hidden = {
                question.id: query.theme_table(
                    conn, question.id, Filter(with_duplicates=True), department_id=department_id
                ).denominator
                - summaries[question.id].denominator
                for question in open_questions
            }
            manifest_questions = tuple(
                _manifest_question(conn, question, theme_sets[question.id], tag_counts[question.id])
                for question in open_questions
            )
            name, run_id, retention_until = _consultation_summary(conn, consultation_id)
            duplicate_answers, duplicate_respondents = _duplicate_counts(conn, consultation_id)
            exported_at = _snapshot_now(conn)
        # Read-only, so there's nothing this loses; closing the
        # transaction here, rather than leaving it open for the caller,
        # is what releases the snapshot.
        conn.commit()
    finally:
        if _in_transaction(conn):
            conn.rollback()
        conn.isolation_level = isolation_level
        conn.read_only = read_only

    manifest = _Manifest(
        consultation_id=consultation_id,
        consultation_name=name,
        run_id=run_id,
        retention_until=retention_until,
        duplicate_answers=duplicate_answers,
        duplicate_respondents=duplicate_respondents,
        questions=manifest_questions,
        exported_at=exported_at,
    )

    workbook = Workbook(write_only=True)
    responses_ws: WriteOnlyWorksheet = workbook.create_sheet("Responses")
    cut = _write_responses(
        responses_ws,
        questions,
        respondent_id_header,
        respondents,
        identity,
        open_questions,
        answers,
        theme_sets,
        tags,
    )
    used_titles = {"Responses".casefold(), "Manifest".casefold()}
    for question in open_questions:
        summary_ws: WriteOnlyWorksheet = workbook.create_sheet(
            _sheet_title(question.column_ref, used_titles)
        )
        cut += _write_summary(summary_ws, summaries[question.id], hidden[question.id])
    manifest_ws: WriteOnlyWorksheet = workbook.create_sheet("Manifest")
    _write_manifest(manifest_ws, manifest, cut)
    workbook.save(path)

    return Exported(
        respondents=len(respondents),
        answers=sum(len(value) for value in answers.values()),
        tags=sum(tag_counts.values()),
        sheets=len(workbook.worksheets),
        truncated_cells=cut,
    )
