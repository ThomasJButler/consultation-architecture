"""The ingest step: the staging table into the schema, in one transaction.

docs/02 step 3a. Rows are read from the staging table in file order and
written as respondents and answers: one answer row per respondent per
question, a multi-select answer one row per chosen option, a blank cell a
row with is_blank set so the denominator can be counted (ADR-004). What a
cell means is the configure step's decision, carried on the question as
its value policy (docs/02, section 3.2): N/A kept or not answered, an
unknown value mapped to an option or not answered. Open answers keep
their text and a hash of it. Identity columns go to the vault schema and
nowhere else, through the ingest role's INSERT-only grant (docs/06,
section 2.4 as corrected). Every insert lands on a key docs/04 section 3
names with ON CONFLICT DO NOTHING, so a second delivery of the ingest
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
from psycopg.types.json import Jsonb

from consult import transitions
from consult.stage import INGEST_ROLE, staging_table
from consult.store import as_role
from consult.tokenise import tokenise

NO_ANSWER = "-"
NOT_APPLICABLE = "N/A"


@dataclass(frozen=True)
class Ingested:
    respondents: int
    answers: int
    vault_rows: int
    duplicate_answers: int
    duplicate_respondents: int
    jobs: int


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


def ingest(
    conn: psycopg.Connection[DictRow], consultation_id: UUID, *, keep_staging: bool = False
) -> Ingested:
    config = _load(conn, consultation_id)
    answered = [q for q in config.questions if q.kind != "identity"]
    identity = [q for q in config.questions if q.kind == "identity"]
    respondents = 0
    answers = 0
    vault_rows = 0
    with as_role(conn, INGEST_ROLE):
        rows = conn.execute(
            sql.SQL("SELECT * FROM {} ORDER BY row_no").format(staging_table(consultation_id))
        ).fetchall()
        for row in rows:
            external_id = (
                row.get(config.respondent_id_column) if config.respondent_id_column else None
            )
            per_question = [
                (question, _answers_for(question, row.get(question.column_ref) or ""))
                for question in answered
            ]
            # The filter read model, cut from the same cells as the answer
            # rows so the two can't drift on the way in: every demographic
            # and closed answer keyed by column, values always arrays, a
            # blank absent (docs/04, section 5).
            attrs: dict[str, list[str]] = {}
            for question, cells in per_question:
                if question.kind in ("demographic", "closed"):
                    values = [value for _option, value, blank, _sha in cells if not blank and value]
                    if values:
                        attrs[question.column_ref] = values
            respondent_id = _respondent(
                conn, config, consultation_id, row["row_no"], external_id or None, attrs
            )
            respondents += 1
            batch = [
                (config.department_id, consultation_id, respondent_id, question.id, *answer)
                for question, cells in per_question
                for answer in cells
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
            vault_rows += _vault(conn, config, respondent_id, row, identity)
        duplicate_answers, duplicate_respondents = _flag_duplicates(conn, consultation_id)
        jobs = _queue_find_themes(conn, consultation_id)
        transitions.mark_processing(conn, consultation_id)
        if not keep_staging:
            # Dropped by the role that created it, in the transaction that
            # emptied it (docs/06, section 2.4 as corrected).
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(staging_table(consultation_id)))
    # docs/02 step 3a ends with ANALYZE, so the first filter query after an
    # ingest plans on statistics for the rows it just wrote. Outside the
    # role block: ANALYZE takes the MAINTAIN privilege in Postgres 17, which
    # the login user has as owner and the ingest role needn't be given.
    conn.execute("ANALYZE respondent, answer")
    return Ingested(
        respondents=respondents,
        answers=answers,
        vault_rows=vault_rows,
        duplicate_answers=duplicate_answers,
        duplicate_respondents=duplicate_respondents,
        jobs=jobs,
    )


def _queue_find_themes(conn: psycopg.Connection[DictRow], consultation_id: UUID) -> int:
    """One find_themes job per open question, pending, on the pass id the
    consultation row holds (docs/04, section 2). job_one_per_run is the
    arbiter, so a replay inserts none; pending to queued is dispatch (PR-08)."""
    return conn.execute(
        """
        INSERT INTO job (department_id, consultation_id, question_id, kind, run_id, status)
        SELECT q.department_id, q.consultation_id, q.id, 'find_themes', c.run_id, 'pending'
          FROM question q JOIN consultation c ON c.id = q.consultation_id
         WHERE q.consultation_id = %s AND q.kind = 'open'
         ORDER BY q.ordinal
        ON CONFLICT DO NOTHING
        """,
        (consultation_id,),
    ).rowcount


def _vault(
    conn: psycopg.Connection[DictRow],
    config: _Configured,
    respondent_id: int,
    row: DictRow,
    identity: list[_Question],
) -> int:
    """Identity columns to the vault and nowhere else: one row per filled
    cell, written as the ingest role, whose only grant on the schema is
    INSERT (docs/06, section 2.4 as corrected). Returns the rows written,
    which on a replay is none."""
    batch = [
        (config.department_id, respondent_id, question.column_ref, cell)
        for question in identity
        if (cell := row.get(question.column_ref) or "") not in ("", NO_ANSWER)
    ]
    if not batch:
        return 0
    # No conflict target on purpose. Naming the key columns makes Postgres
    # check SELECT on them as well as INSERT, and the ingest role holds only
    # INSERT here; the bare form infers nothing and needs nothing more
    # (measured in this repo on PostgreSQL 17.11, 25 September 2026).
    cursor = conn.cursor()
    cursor.executemany(
        """
        INSERT INTO vault.respondent_identity (department_id, respondent_id, column_ref, value_text)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        batch,
    )
    return cursor.rowcount


def _flag_duplicates(conn: psycopg.Connection[DictRow], consultation_id: UUID) -> tuple[int, int]:
    """Exact duplicates, flagged and counted, never deleted (docs/02,
    section 7, decision 9). At answer level: same question, same normalised
    text, every copy pointing at the first. At respondent level: every open
    answer identical to an earlier respondent's, blanks and all, and not
    all blank: the shape of a campaign proforma. Both computed over the rows
    just written, so a replay finds them already flagged and does nothing."""
    answers = conn.execute(
        """
        UPDATE answer a SET duplicate_of_answer_id = f.first_id
          FROM (SELECT question_id, text_sha256, min(id) AS first_id
                  FROM answer
                 WHERE consultation_id = %(id)s AND text_sha256 IS NOT NULL
                 GROUP BY question_id, text_sha256
                HAVING count(*) > 1) f
         WHERE a.consultation_id = %(id)s AND a.question_id = f.question_id
           AND a.text_sha256 = f.text_sha256 AND a.id <> f.first_id
           AND a.duplicate_of_answer_id IS NULL
        """,
        {"id": consultation_id},
    ).rowcount
    respondents = conn.execute(
        """
        WITH signatures AS (
            SELECT a.respondent_id,
                   string_agg(coalesce(encode(a.text_sha256, 'hex'), ''), '|' ORDER BY q.ordinal)
                       AS signature,
                   bool_or(a.text_sha256 IS NOT NULL) AS answered
              FROM answer a JOIN question q ON q.id = a.question_id
             WHERE a.consultation_id = %(id)s AND q.kind = 'open'
             GROUP BY a.respondent_id),
        firsts AS (
            SELECT signature, min(respondent_id) AS first_id
              FROM signatures WHERE answered
             GROUP BY signature HAVING count(*) > 1)
        UPDATE respondent r SET duplicate_of = f.first_id
          FROM signatures s JOIN firsts f ON f.signature = s.signature
         WHERE r.id = s.respondent_id AND r.id <> f.first_id AND r.duplicate_of IS NULL
        """,
        {"id": consultation_id},
    ).rowcount
    return answers, respondents


def _respondent(
    conn: psycopg.Connection[DictRow],
    config: _Configured,
    consultation_id: UUID,
    row_no: int,
    external_id: str | None,
    attrs: dict[str, list[str]],
) -> int:
    inserted = conn.execute(
        """
        INSERT INTO respondent (department_id, consultation_id, external_id, source_row_no, attrs)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (consultation_id, source_row_no) DO NOTHING
        RETURNING id
        """,
        (config.department_id, consultation_id, external_id, row_no, Jsonb(attrs)),
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
