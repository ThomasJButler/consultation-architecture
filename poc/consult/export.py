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

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import psycopg
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import Cell
from openpyxl.worksheet._write_only import WriteOnlyWorksheet
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
# the cursor (the last id seen) is read in, looped until a page is short.
_KEYSET_PAGE = 1000


def neutralise(value: str) -> str:
    """A value starting with a formula trigger gets a leading apostrophe,
    the convention every spreadsheet reads as "force this cell to text"
    (docs/06, section 2.7). The file's own no-answer marker, a lone "-",
    is left alone: it isn't a formula wherever it's read back, and
    prefixing it would change what "not answered" looks like (docs/00).
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


def _cell(ws: WriteOnlyWorksheet, value: object) -> Cell:
    """Every cell as text, twice over (docs/06, section 2.7): `neutralise`
    above, and `data_type` forced to "s" on top of it, since openpyxl
    infers "f" from a leading "=" on a plain string otherwise (measured
    against openpyxl 3.1.5, 26 September 2026).
    """
    text = "" if value is None else str(value)
    cell = WriteOnlyCell(ws, value=neutralise(text))
    cell.data_type = "s"
    return cell


def _row(ws: WriteOnlyWorksheet, values: list[object]) -> None:
    ws.append([_cell(ws, value) for value in values])


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
    whole answer set. The first read `write_workbook` makes, which is why
    test_export.py's test_export_reads_as_the_export_role wraps this call
    rather than another one."""
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
    page an index scan rather than a re-sort."""
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
    keys = conn.execute(
        "SELECT key FROM theme WHERE theme_set_version_id = %s ORDER BY key",
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
    # Always a batch of one (mapping._send retries a refused batch at size
    # one before calling it unprocessable), so counting batches counts
    # answers.
    unprocessable = conn.execute(
        "SELECT count(*) AS n FROM job_batch jb JOIN job j ON j.id = jb.job_id"
        " WHERE j.question_id = %s AND jb.status = 'unprocessable'",
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
) -> None:
    """docs/02 step 12's first sheet: the respondent id, the vault's
    identity column, every demographic and closed answer from
    `respondent.attrs`, every open question's text, then one column per
    theme per open question (docs/04, section 6)."""
    other_questions = [question for question in questions if question.kind != "open"]

    header: list[object] = []
    if respondent_id_header is not None:
        header.append(respondent_id_header)
    header.extend(question.column_ref for question in other_questions)
    header.extend(question.column_ref for question in open_questions)
    for question in open_questions:
        header.extend(f"{question.column_ref}: {key}" for key in theme_sets[question.id].keys)
    _row(ws, header)

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
        _row(ws, values)


def _write_summary(ws: WriteOnlyWorksheet, table: query.ThemeTable) -> None:
    """docs/02 step 12's per-question summary: key, label, respondents and
    the denominator, straight from `query.theme_table` under the default
    filter, so this number and the per-question dashboard's can't drift
    apart."""
    _row(ws, ["key", "label", "respondents", "denominator"])
    for row in table.rows:
        _row(ws, [row.key, row.label, row.respondents, table.denominator])


def _write_manifest(ws: WriteOnlyWorksheet, manifest: _Manifest) -> None:
    """docs/02 step 12's manifest, cut to what this schema holds: no
    prompt hash and no agreement rate, since neither has a column here."""
    _row(ws, ["field", "value"])
    _row(ws, ["consultation_id", manifest.consultation_id])
    _row(ws, ["consultation_name", manifest.consultation_name])
    _row(ws, ["run_id", manifest.run_id])
    _row(ws, ["retention_until", _or_dash(manifest.retention_until)])
    _row(ws, ["exported_at", manifest.exported_at.isoformat()])
    _row(ws, ["duplicate_answers", manifest.duplicate_answers])
    _row(ws, ["duplicate_respondents", manifest.duplicate_respondents])
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


@dataclass(frozen=True)
class Exported:
    respondents: int
    answers: int
    tags: int
    sheets: int


def write_workbook(
    conn: psycopg.Connection[DictRow], consultation_id: UUID, path: Path
) -> Exported:
    """docs/02 step 12's XLSX: the Responses sheet, one summary sheet per
    open question and a manifest, saved to `path`. The reads run as
    `store.EXPORT_ROLE` (docs/06, section 2.4 as corrected), the one role
    with a grant on the vault and none on the pipeline's writes.
    """
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
        summaries = {
            question.id: query.theme_table(conn, question.id, Filter())
            for question in open_questions
        }
        manifest_questions = tuple(
            _manifest_question(conn, question, theme_sets[question.id], tag_counts[question.id])
            for question in open_questions
        )
        name, run_id, retention_until = _consultation_summary(conn, consultation_id)
        duplicate_answers, duplicate_respondents = _duplicate_counts(conn, consultation_id)

    manifest = _Manifest(
        consultation_id=consultation_id,
        consultation_name=name,
        run_id=run_id,
        retention_until=retention_until,
        duplicate_answers=duplicate_answers,
        duplicate_respondents=duplicate_respondents,
        questions=manifest_questions,
        exported_at=datetime.now(UTC),
    )

    workbook = Workbook(write_only=True)
    responses_ws: WriteOnlyWorksheet = workbook.create_sheet("Responses")
    _write_responses(
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
    for question in open_questions:
        summary_ws: WriteOnlyWorksheet = workbook.create_sheet(f"{question.column_ref} summary")
        _write_summary(summary_ws, summaries[question.id])
    manifest_ws: WriteOnlyWorksheet = workbook.create_sheet("Manifest")
    _write_manifest(manifest_ws, manifest)
    workbook.save(path)

    return Exported(
        respondents=len(respondents),
        answers=sum(len(value) for value in answers.values()),
        tags=sum(tag_counts.values()),
        sheets=len(workbook.worksheets),
    )
