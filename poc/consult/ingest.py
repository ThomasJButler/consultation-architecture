"""The ingest step: the staging table into the schema, in one transaction.

docs/02 step 3a. Rows are read from the staging table in file order and
written as respondents and answers: one answer row per respondent per
question, a multi-select answer one row per chosen option, a blank cell a
row with is_blank set so the denominator can be counted (ADR-004). What a
cell means is the configure step's decision, carried on the question as
its value policy (docs/02, section 3.2): N/A kept or not answered, an
unknown value mapped to an option or not answered. Open answers keep
their text and a hash of it. Every insert lands on a key docs/04 section
3 names with ON CONFLICT DO NOTHING, so a second delivery of the ingest
message finds the rows already there. Runs as the ingest role. Nothing
here commits.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import DictRow

from consult.stage import INGEST_ROLE, staging_table
from consult.store import as_role
from consult.tokenise import tokenise

NO_ANSWER = "-"
NOT_APPLICABLE = "N/A"


@dataclass(frozen=True)
class Ingested:
    respondents: int
    answers: int


@dataclass(frozen=True)
class _Question:
    id: UUID
    column_ref: str
    kind: str
    response_type: str | None
    options: dict[str, UUID]
    keep_not_applicable: bool
    mappings: dict[str, str | None]


@dataclass(frozen=True)
class _Configured:
    department_id: UUID
    questions: tuple[_Question, ...]
    respondent_id_column: str | None


def normalised_sha256(text: str) -> bytes:
    """The duplicate key for an answer: whitespace collapsed, case folded,
    so two copies of a proforma that differ by a line break still match."""
    return hashlib.sha256(" ".join(text.split()).casefold().encode()).digest()


def _load(conn: psycopg.Connection[DictRow], consultation_id: UUID) -> _Configured:
    consultation = conn.execute(
        "SELECT department_id, column_roles FROM consultation WHERE id = %s", (consultation_id,)
    ).fetchone()
    if consultation is None:
        raise LookupError(f"consultation {consultation_id} does not exist")
    rows = conn.execute(
        """
        SELECT q.id, q.column_ref, q.kind, q.response_type, q.value_policy,
               coalesce(json_object_agg(o.label, o.id) FILTER (WHERE o.id IS NOT NULL), '{}') AS options
          FROM question q LEFT JOIN question_option o ON o.question_id = q.id
         WHERE q.consultation_id = %s
         GROUP BY q.id ORDER BY q.ordinal
        """,
        (consultation_id,),
    ).fetchall()
    questions = tuple(
        _Question(
            id=row["id"],
            column_ref=row["column_ref"],
            kind=row["kind"],
            response_type=row["response_type"],
            options={label: UUID(option_id) for label, option_id in row["options"].items()},
            keep_not_applicable=row["value_policy"].get("not_applicable")
            != "treat_as_not_answered",
            mappings=dict(row["value_policy"].get("unknown_values", {})),
        )
        for row in rows
    )
    roles = consultation["column_roles"] or {}
    return _Configured(consultation["department_id"], questions, roles.get("respondent_id"))


Answer = tuple[UUID | None, str | None, bool, bytes | None]


def _answers_for(question: _Question, cell: str) -> list[Answer]:
    """(option_id, value_text, is_blank, text_sha256) rows for one cell."""
    if cell in ("", NO_ANSWER):
        return [(None, None, True, None)]
    if question.kind == "open":
        if cell == NOT_APPLICABLE:
            return [(None, None, True, None)]
        return [(None, cell, False, normalised_sha256(cell))]
    if cell == NOT_APPLICABLE:
        return (
            [(None, cell, False, None)]
            if question.keep_not_applicable
            else [(None, None, True, None)]
        )
    if question.kind == "demographic":
        return [(None, cell, False, None)]
    if question.response_type == "multi_select":
        tokenised = tokenise(cell, tuple(question.options))
        labels = [*tokenised.tokens]
        for unknown in tokenised.unknown:
            mapped = question.mappings.get(unknown)
            if mapped is not None:
                labels.append(mapped)
        if not labels:
            return [(None, None, True, None)]
        return [(question.options[label], label, False, None) for label in dict.fromkeys(labels)]
    label = cell if cell in question.options else question.mappings.get(cell)
    if label is None or label not in question.options:
        return [(None, None, True, None)]
    return [(question.options[label], label, False, None)]


def ingest(conn: psycopg.Connection[DictRow], consultation_id: UUID) -> Ingested:
    config = _load(conn, consultation_id)
    answered = [q for q in config.questions if q.kind != "identity"]
    respondents = 0
    answers = 0
    with as_role(conn, INGEST_ROLE):
        rows = conn.execute(
            sql.SQL("SELECT * FROM {} ORDER BY row_no").format(staging_table(consultation_id))
        ).fetchall()
        for row in rows:
            external_id = (
                row.get(config.respondent_id_column) if config.respondent_id_column else None
            )
            respondent_id = _respondent(
                conn, config, consultation_id, row["row_no"], external_id or None
            )
            respondents += 1
            batch = [
                (config.department_id, consultation_id, respondent_id, question.id, *answer)
                for question in answered
                for answer in _answers_for(question, row.get(question.column_ref) or "")
            ]
            conn.cursor().executemany(
                """
                INSERT INTO answer (department_id, consultation_id, respondent_id, question_id,
                                    option_id, value_text, is_blank, text_sha256)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (respondent_id, question_id, option_id) DO NOTHING
                """,
                batch,
            )
            answers += len(batch)
    return Ingested(respondents, answers)


def _respondent(
    conn: psycopg.Connection[DictRow],
    config: _Configured,
    consultation_id: UUID,
    row_no: int,
    external_id: str | None,
) -> int:
    inserted = conn.execute(
        """
        INSERT INTO respondent (department_id, consultation_id, external_id, source_row_no)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (consultation_id, source_row_no) DO NOTHING
        RETURNING id
        """,
        (config.department_id, consultation_id, external_id, row_no),
    ).fetchone()
    if inserted is not None:
        return int(inserted["id"])
    existing = conn.execute(
        "SELECT id FROM respondent WHERE consultation_id = %s AND source_row_no = %s",
        (consultation_id, row_no),
    ).fetchone()
    if existing is None:
        raise LookupError(f"consultation {consultation_id}: row {row_no} vanished mid-ingest")
    return int(existing["id"])
