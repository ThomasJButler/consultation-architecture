"""What the ingest step promises (docs/02, step 3a; ADR-004; docs/04).

One transaction turns the staging table into the long answer table, the
respondents with their filter documents, the vault rows and the jobs.
Every expectation here is read from the fixture CSV, not from the code.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import DictRow

from consult.ingest import ingest
from consult.tokenise import tokenise
from tests.pipeline import NOT_ANSWERED, fixture_rows, staged_fixture

pytestmark = pytest.mark.db


def test_ingest_explodes_answers_one_row_per_option(db: psycopg.Connection[DictRow]) -> None:
    rows = fixture_rows()
    staged = staged_fixture(db)
    questions = staged.configured.questions

    result = ingest(db, staged.consultation_id)

    assert result.respondents == len(rows) == 240
    respondents = db.execute(
        "SELECT source_row_no, external_id FROM respondent WHERE consultation_id = %s ORDER BY id",
        (staged.consultation_id,),
    ).fetchall()
    assert [(r["source_row_no"], r["external_id"]) for r in respondents[:2]] == [
        (2, "R-0001"),
        (3, "R-0002"),
    ]

    def count(question: str, where: str = "true") -> int:
        row = db.execute(
            f"SELECT count(*) AS n FROM answer WHERE question_id = %s AND {where}",  # noqa: S608
            (questions[question],),
        ).fetchone()
        assert row is not None
        return int(row["n"])

    # One row per respondent for a demographic column, blanks included.
    assert count("d_area") == 240
    assert count("d_area", "is_blank") == sum(row["d_area"] in NOT_ANSWERED for row in rows)
    # N/A stays a value on a demographic column under the default policy.
    assert count("d_commute", "value_text = 'N/A' AND NOT is_blank") == sum(
        row["d_commute"] == "N/A" for row in rows
    )
    # A single-select answer is one row with its option; an unknown value
    # under the default resolution is not answered.
    assert count("c_route", "option_id IS NOT NULL") == sum(
        row["c_route"] in {"Support", "Oppose", "Not sure"} for row in rows
    )
    assert count("c_route", "is_blank") == sum(
        row["c_route"] in NOT_ANSWERED | {"Unsure"} for row in rows
    )
    # A multi-select answer is one row per chosen option, the comma option
    # one of them; a blank cell is one blank row.
    vocabulary = ("Cycle", "Walk", "Run", "Wheelchair, mobility scooter or similar", "Push a pram")
    chosen = sum(
        len(tokenise(row["c_modes"], vocabulary).tokens)
        for row in rows
        if row["c_modes"] not in NOT_ANSWERED
    )
    assert count("c_modes", "option_id IS NOT NULL") == chosen
    assert count("c_modes", "is_blank") == sum(row["c_modes"] in NOT_ANSWERED for row in rows)
    # Open answers keep their text and get a hash; N/A on an open question
    # is not answered (docs/02, section 3.2).
    assert count(
        "o_reason", "NOT is_blank AND text_sha256 IS NOT NULL AND value_text IS NOT NULL"
    ) == sum(row["o_reason"] not in NOT_ANSWERED | {"N/A"} for row in rows)
    assert count("o_reason", "is_blank") == sum(
        row["o_reason"] in NOT_ANSWERED | {"N/A"} for row in rows
    )
    first = db.execute(
        """
        SELECT a.value_text FROM answer a JOIN respondent r ON r.id = a.respondent_id
         WHERE a.question_id = %s AND r.source_row_no = 2
        """,
        (questions["o_reason"],),
    ).fetchone()
    assert first == {"value_text": rows[0]["o_reason"]}
    # An identity column gets no answer rows (docs/04, section 1).
    assert count("email") == 0
    total = db.execute(
        "SELECT count(*) AS n FROM answer WHERE consultation_id = %s", (staged.consultation_id,)
    ).fetchone()
    assert total == {"n": result.answers}


def test_ingest_builds_attrs_that_match_the_answer_rows(db: psycopg.Connection[DictRow]) -> None:
    rows = fixture_rows()
    staged = staged_fixture(db)
    ingest(db, staged.consultation_id)

    # docs/04 section 5: every demographic and closed answer keyed by column,
    # values always arrays, N/A kept as a value, a skipped column absent,
    # nothing from an open or identity column.
    vocabulary = ("Cycle", "Walk", "Run", "Wheelchair, mobility scooter or similar", "Push a pram")
    expected: dict[int, dict[str, list[str]]] = {}
    for no, row in enumerate(rows, start=2):
        document: dict[str, list[str]] = {}
        for ref in ("d_area", "d_commute", "d_age"):
            if row[ref] not in NOT_ANSWERED:
                document[ref] = [row[ref]]
        if row["c_route"] in {"Support", "Oppose", "Not sure"}:
            document["c_route"] = [row["c_route"]]
        if row["c_modes"] not in NOT_ANSWERED:
            tokens = tokenise(row["c_modes"], vocabulary).tokens
            if tokens:
                document["c_modes"] = sorted(tokens)
        if row["c_safety"] not in NOT_ANSWERED:
            document["c_safety"] = [row["c_safety"]]
        expected[no] = document
    written = db.execute(
        "SELECT source_row_no, attrs FROM respondent WHERE consultation_id = %s",
        (staged.consultation_id,),
    ).fetchall()
    assert {
        r["source_row_no"]: {key: sorted(values) for key, values in r["attrs"].items()}
        for r in written
    } == expected

    # ADR-004's promised check: attrs rebuilt from the answer rows equals
    # what ingest wrote, for every respondent.
    rebuilt = db.execute(
        """
        SELECT a.respondent_id, jsonb_object_agg(q.column_ref, v.values) AS attrs
          FROM (SELECT respondent_id, question_id, jsonb_agg(value_text ORDER BY value_text) AS values
                  FROM answer WHERE consultation_id = %s AND NOT is_blank
                 GROUP BY respondent_id, question_id) v
          JOIN answer a ON a.respondent_id = v.respondent_id AND a.question_id = v.question_id
          JOIN question q ON q.id = v.question_id AND q.kind IN ('demographic', 'closed')
         GROUP BY a.respondent_id
        """,
        (staged.consultation_id,),
    ).fetchall()
    stored = {
        r["respondent_id"]: {key: sorted(values) for key, values in r["attrs"].items()}
        for r in db.execute(
            "SELECT id AS respondent_id, attrs FROM respondent WHERE consultation_id = %s AND attrs <> '{}'",
            (staged.consultation_id,),
        ).fetchall()
    }
    assert {r["respondent_id"]: r["attrs"] for r in rebuilt} == stored
