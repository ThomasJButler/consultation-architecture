"""What the ingest step promises (docs/02, step 3a; ADR-004; docs/04).

One transaction turns the staging table into the long answer table, the
respondents with their filter documents, the vault rows and the jobs.
Every expectation here is read from the fixture CSV, not from the code.
"""

from __future__ import annotations

import csv
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import DictRow

from consult.ingest import Ingested, IngestError, ingest
from consult.stage import stage, staging_table
from consult.validate import Resolution, WarningKind
from tests.pipeline import NOT_ANSWERED, RESPONSES, fixture_rows, staged_fixture
from tests.rows import make_consultation, make_department

pytestmark = pytest.mark.db

NOTHING_WRITTEN = Ingested(
    respondents=0, answers=0, vault_rows=0, duplicate_answers=0, duplicate_respondents=0, jobs=0
)


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
    # one of them; a blank cell is one blank row. Chosen options are
    # counted as substrings of the cell, which is independent of the
    # tokeniser ingest uses and exact here because no label contains
    # another.
    vocabulary = ("Cycle", "Walk", "Run", "Wheelchair, mobility scooter or similar", "Push a pram")
    chosen = sum(
        len([label for label in vocabulary if label in row["c_modes"]])
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
            tokens = [label for label in vocabulary if label in row["c_modes"]]
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


def normalised(text: str) -> str:
    return " ".join(text.split()).casefold()


def test_ingest_flags_duplicates_at_answer_and_respondent_level(
    db: psycopg.Connection[DictRow],
) -> None:
    rows = fixture_rows()
    staged = staged_fixture(db)
    questions = staged.configured.questions

    result = ingest(db, staged.consultation_id)

    # Answer level: same question, identical normalised text; every copy but
    # the first points at the first (docs/02, step 3a).
    expected_answer_duplicates: dict[tuple[str, int], int] = {}
    for ref in ("o_reason", "o_safety"):
        firsts: dict[str, int] = {}
        for no, row in enumerate(rows, start=2):
            text = row[ref]
            if text in NOT_ANSWERED | {"N/A"}:
                continue
            key = normalised(text)
            if key in firsts:
                expected_answer_duplicates[ref, no] = firsts[key]
            else:
                firsts[key] = no
    written = db.execute(
        """
        SELECT q.column_ref, r.source_row_no, f.source_row_no AS first_row_no
          FROM answer a
          JOIN question q ON q.id = a.question_id
          JOIN respondent r ON r.id = a.respondent_id
          JOIN answer d ON d.id = a.duplicate_of_answer_id
          JOIN respondent f ON f.id = d.respondent_id
         WHERE a.consultation_id = %s AND a.question_id = ANY(%s)
        """,
        (staged.consultation_id, [questions["o_reason"], questions["o_safety"]]),
    ).fetchall()
    assert {
        (w["column_ref"], w["source_row_no"]): w["first_row_no"] for w in written
    } == expected_answer_duplicates
    assert result.duplicate_answers == len(expected_answer_duplicates)
    assert len(expected_answer_duplicates) >= 11, (
        "the proforma alone gives eleven copies per column"
    )

    # Respondent level: every open answer identical to an earlier
    # respondent's, blanks and all, and not all blank: a campaign proforma.
    expected_respondent_duplicates: dict[int, int] = {}
    seen: dict[tuple[str, ...], int] = {}
    for no, row in enumerate(rows, start=2):
        signature = tuple(
            "" if row[ref] in NOT_ANSWERED | {"N/A"} else normalised(row[ref])
            for ref in ("o_reason", "o_safety")
        )
        if not any(signature):
            continue
        if signature in seen:
            expected_respondent_duplicates[no] = seen[signature]
        else:
            seen[signature] = no
    flagged = db.execute(
        """
        SELECT r.source_row_no, f.source_row_no AS first_row_no
          FROM respondent r JOIN respondent f ON f.id = r.duplicate_of
         WHERE r.consultation_id = %s
        """,
        (staged.consultation_id,),
    ).fetchall()
    assert {
        f["source_row_no"]: f["first_row_no"] for f in flagged
    } == expected_respondent_duplicates
    assert result.duplicate_respondents == len(expected_respondent_duplicates)
    assert len(expected_respondent_duplicates) >= 11

    # Nothing deleted: counted both ways, kept both ways.
    kept = db.execute(
        "SELECT count(*) AS n FROM respondent WHERE consultation_id = %s", (staged.consultation_id,)
    ).fetchone()
    assert kept == {"n": 240}


def staging_tables(db: psycopg.Connection[DictRow]) -> list[str]:
    return [
        r["tablename"]
        for r in db.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'staging' ORDER BY 1"
        ).fetchall()
    ]


def test_ingest_inserts_one_find_themes_job_per_open_question(
    db: psycopg.Connection[DictRow],
) -> None:
    staged = staged_fixture(db)
    before = db.execute(
        "SELECT status FROM consultation WHERE id = %s", (staged.consultation_id,)
    ).fetchone()
    assert before == {"status": "staged"}

    result = ingest(db, staged.consultation_id)

    # One pending find_themes job per open question, carrying the pass id
    # the consultation row holds (docs/04, section 2), nothing claimed:
    # dispatch to queued is PR-08's.
    jobs = db.execute(
        """
        SELECT q.column_ref, j.kind, j.status, j.run_id = c.run_id AS this_pass, j.attempts,
               j.claimed_by, j.department_id = c.department_id AS scoped
          FROM job j
          JOIN question q ON q.id = j.question_id
          JOIN consultation c ON c.id = j.consultation_id
         WHERE j.consultation_id = %s
         ORDER BY q.ordinal
        """,
        (staged.consultation_id,),
    ).fetchall()
    assert jobs == [
        {
            "column_ref": ref,
            "kind": "find_themes",
            "status": "pending",
            "this_pass": True,
            "attempts": 0,
            "claimed_by": None,
            "scoped": True,
        }
        for ref in ("o_reason", "o_safety")
    ]
    assert result.jobs == 2
    # The consultation is processing and its staging table is gone, in the
    # same transaction (docs/02, step 3a).
    after = db.execute(
        "SELECT status FROM consultation WHERE id = %s", (staged.consultation_id,)
    ).fetchone()
    assert after == {"status": "processing"}
    assert staging_tables(db) == []
    # A caller that wants to look at the table afterwards can keep it.
    kept = staged_fixture(db)
    ingest(db, kept.consultation_id, keep_staging=True)
    assert staging_tables(db) == [str(kept.consultation_id)]


def snapshot(db: psycopg.Connection[DictRow], consultation_id: UUID) -> dict[str, list[DictRow]]:
    """Every row ingest is responsible for, in a stable order, so two runs
    can be compared whole."""
    queries = {
        "respondents": """
            SELECT id, external_id, source_row_no, attrs, duplicate_of
              FROM respondent WHERE consultation_id = %s ORDER BY id""",
        "answers": """
            SELECT id, respondent_id, question_id, option_id, value_text, is_blank, text_sha256,
                   duplicate_of_answer_id
              FROM answer WHERE consultation_id = %s ORDER BY id""",
        "identity": """
            SELECT v.respondent_id, v.column_ref, v.value_text
              FROM vault.respondent_identity v JOIN respondent r ON r.id = v.respondent_id
             WHERE r.consultation_id = %s ORDER BY 1, 2""",
        "jobs": """
            SELECT id, question_id, kind, status, run_id
              FROM job WHERE consultation_id = %s ORDER BY id""",
        "consultation": "SELECT status, run_id FROM consultation WHERE id = %s",
    }
    return {
        name: db.execute(query, (consultation_id,)).fetchall() for name, query in queries.items()
    }


def test_ingest_is_idempotent_on_replay(db: psycopg.Connection[DictRow]) -> None:
    staged = staged_fixture(db)
    nothing_written = NOTHING_WRITTEN

    first = ingest(db, staged.consultation_id, keep_staging=True)
    assert (first.respondents, first.vault_rows, first.jobs) == (240, 240, 2)
    written = snapshot(db, staged.consultation_id)

    # Delivered again with the table still there: the same rows, and the
    # counts say nothing was written (docs/04, section 3).
    assert ingest(db, staged.consultation_id, keep_staging=True) == nothing_written
    assert snapshot(db, staged.consultation_id) == written
    # Delivered again after the drop, which is what a redelivery after the
    # first run's commit looks like: the consultation is already processing
    # and the table is gone, so the job finishes quietly.
    assert ingest(db, staged.consultation_id) == nothing_written
    assert staging_tables(db) == []
    assert ingest(db, staged.consultation_id) == nothing_written
    assert snapshot(db, staged.consultation_id) == written


def write_repeated_id_file(tmp_path: Path) -> Path:
    """The fixture with row 3's respondent id changed to row 2's."""
    rows = fixture_rows()
    rows[1]["respondent_ref"] = rows[0]["respondent_ref"]
    path = tmp_path / "repeated.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def external_ids(db: psycopg.Connection[DictRow], consultation_id: UUID) -> dict[int, str | None]:
    return {
        r["source_row_no"]: r["external_id"]
        for r in db.execute(
            "SELECT source_row_no, external_id FROM respondent WHERE consultation_id = %s",
            (consultation_id,),
        ).fetchall()
    }


def test_a_repeated_respondent_id_is_refused_by_ingest_unless_resolved(
    db: psycopg.Connection[DictRow], tmp_path: Path
) -> None:
    path = write_repeated_id_file(tmp_path)

    # Unresolved: refused before a row is written, naming the rows and never
    # the value (docs/04, section 3; docs/02, section 3.2 as corrected).
    unresolved = staged_fixture(db, path=path, resolve=lambda r: replace(r, duplicate_ids=None))
    assert any(w.kind is WarningKind.DUPLICATE_RESPONDENT_ID for w in unresolved.report.warnings)
    with pytest.raises(IngestError) as refused:
        ingest(db, unresolved.consultation_id)
    assert "rows 2, 3" in str(refused.value)
    assert "R-0001" not in str(refused.value)
    assert external_ids(db, unresolved.consultation_id) == {}
    status = db.execute(
        "SELECT status FROM consultation WHERE id = %s", (unresolved.consultation_id,)
    ).fetchone()
    assert status == {"status": "staged"}

    # The validator's default: keep the id on the first occurrence, blank
    # it on the rest, drop no row.
    kept = staged_fixture(db, path=path)
    assert kept.resolutions.duplicate_ids is Resolution.KEEP_FIRST_BLANK_REST
    ingest(db, kept.consultation_id)
    ids = external_ids(db, kept.consultation_id)
    assert len(ids) == 240
    assert (ids[2], ids[3], ids[4]) == ("R-0001", None, "R-0003")

    # Or ignore the column: every row identified by its row number alone.
    ignored = staged_fixture(
        db, path=path, resolve=lambda r: replace(r, duplicate_ids=Resolution.IGNORE_COLUMN)
    )
    ingest(db, ignored.consultation_id)
    ids = external_ids(db, ignored.consultation_id)
    assert len(ids) == 240
    assert set(ids.values()) == {None}


def test_a_dash_in_the_id_column_is_no_id(db: psycopg.Connection[DictRow], tmp_path: Path) -> None:
    # `-` is not answered in every column (docs/02, section 3.2), the id
    # column included: the validator doesn't count two dashes as a repeated
    # id, so ingest mustn't refuse them as one. Found by the security review.
    rows = fixture_rows()
    rows[0]["respondent_ref"] = "-"
    rows[1]["respondent_ref"] = "-"
    path = tmp_path / "dashes.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    staged = staged_fixture(db, path=path)
    assert not any(w.kind is WarningKind.DUPLICATE_RESPONDENT_ID for w in staged.report.warnings)

    result = ingest(db, staged.consultation_id)

    assert result.respondents == 240
    ids = external_ids(db, staged.consultation_id)
    assert (ids[2], ids[3], ids[4]) == (None, None, "R-0003")


def test_ingest_refuses_the_wrong_state_nothing_configured_and_a_lost_table(
    db: psycopg.Connection[DictRow],
) -> None:
    # Found by the review: with nothing configured, ingest wrote 240 empty
    # respondents, set processing and dropped the only copy of the answers.
    department_id = make_department(db)
    draft = make_consultation(db, department_id)
    with pytest.raises(IngestError, match="draft"):
        ingest(db, draft)

    unconfigured = make_consultation(db, department_id)
    stage(db, unconfigured, RESPONSES)
    with pytest.raises(IngestError, match="no questions"):
        ingest(db, unconfigured)
    assert staging_tables(db) == [str(unconfigured)]
    status = db.execute("SELECT status FROM consultation WHERE id = %s", (unconfigured,)).fetchone()
    assert status == {"status": "staged"}

    # A staged consultation whose table has gone: the production path
    # re-stages from the upload (docs/02, correction 5); this
    # proof-of-concept has no upload to re-stage from, so it says so
    # instead of failing on the SELECT.
    lost = staged_fixture(db)
    db.execute(sql.SQL("DROP TABLE {}").format(staging_table(lost.consultation_id)))
    with pytest.raises(IngestError, match="staging table"):
        ingest(db, lost.consultation_id)


def test_a_redelivery_after_the_consultation_moved_on_finishes_quietly(
    db: psycopg.Connection[DictRow],
) -> None:
    # A fast find_themes can take the consultation past processing before
    # the queue redelivers the ingest message; the redelivery still has
    # nothing to do (docs/04, section 3). Found by the review.
    staged = staged_fixture(db)
    ingest(db, staged.consultation_id)
    for status in ("awaiting_review", "ready"):
        db.execute(
            "UPDATE consultation SET status = %s WHERE id = %s", (status, staged.consultation_id)
        )
        assert ingest(db, staged.consultation_id) == NOTHING_WRITTEN


def test_the_not_applicable_policy_can_make_na_not_answered(
    db: psycopg.Connection[DictRow],
) -> None:
    # docs/02 section 3.2: N/A is a real value on a demographic column
    # unless the reviewer says otherwise at configure time. Every other
    # test keeps it; this one turns it off for d_commute and reads the
    # consequences off the policy, the answer rows and attrs.
    rows = fixture_rows()
    staged = staged_fixture(
        db, resolve=lambda r: replace(r, not_applicable={**r.not_applicable, "d_commute": False})
    )
    ingest(db, staged.consultation_id)

    question_id = staged.configured.questions["d_commute"]
    policy = db.execute(
        "SELECT value_policy FROM question WHERE id = %s", (question_id,)
    ).fetchone()
    assert policy == {"value_policy": {"not_applicable": "treat_as_not_answered"}}
    not_applicable = sum(row["d_commute"] == "N/A" for row in rows)
    not_answered = sum(row["d_commute"] in NOT_ANSWERED for row in rows)
    assert not_applicable > 0
    counts = db.execute(
        """
        SELECT count(*) FILTER (WHERE is_blank AND value_text IS NULL) AS blank,
               count(*) FILTER (WHERE value_text = 'N/A') AS kept
          FROM answer WHERE question_id = %s
        """,
        (question_id,),
    ).fetchone()
    assert counts == {"blank": not_applicable + not_answered, "kept": 0}
    without = db.execute(
        "SELECT count(*) AS n FROM respondent WHERE consultation_id = %s AND NOT attrs ? 'd_commute'",
        (staged.consultation_id,),
    ).fetchone()
    assert without == {"n": not_applicable + not_answered}


def test_a_mapped_or_added_unknown_value_lands_on_its_option(
    db: psycopg.Connection[DictRow],
) -> None:
    # docs/02 section 3.2: an unknown value is mapped to an option that
    # exists, added as a new one, or not answered. The defaults take the
    # last; these are the other two, read off the answer rows and attrs.
    rows = fixture_rows()
    unsure = sum(row["c_route"] == "Unsure" for row in rows)
    assert unsure > 0
    first_unsure = next(no for no, row in enumerate(rows, start=2) if row["c_route"] == "Unsure")

    mapped = staged_fixture(
        db,
        resolve=lambda r: replace(
            r, unknown_values={**r.unknown_values, ("c_route", "Unsure"): "Not sure"}
        ),
    )
    ingest(db, mapped.consultation_id)
    question_id = mapped.configured.questions["c_route"]
    counts = db.execute(
        """
        SELECT count(*) FILTER (WHERE value_text = 'Not sure') AS not_sure,
               count(*) FILTER (WHERE option_id IS NOT NULL) AS answered
          FROM answer WHERE question_id = %s
        """,
        (question_id,),
    ).fetchone()
    assert counts == {
        "not_sure": sum(row["c_route"] in {"Not sure", "Unsure"} for row in rows),
        "answered": sum(
            row["c_route"] in {"Support", "Oppose", "Not sure", "Unsure"} for row in rows
        ),
    }
    attrs = db.execute(
        "SELECT attrs -> 'c_route' AS route FROM respondent WHERE consultation_id = %s AND source_row_no = %s",
        (mapped.consultation_id, first_unsure),
    ).fetchone()
    assert attrs == {"route": ["Not sure"]}

    added = staged_fixture(
        db,
        resolve=lambda r: replace(
            r, unknown_values={**r.unknown_values, ("c_route", "Unsure"): "Unsure"}
        ),
    )
    ingest(db, added.consultation_id)
    question_id = added.configured.questions["c_route"]
    labels = [
        r["label"]
        for r in db.execute(
            "SELECT label FROM question_option WHERE question_id = %s ORDER BY ordinal",
            (question_id,),
        ).fetchall()
    ]
    assert labels == ["Support", "Oppose", "Not sure", "Unsure"]
    landed = db.execute(
        "SELECT count(*) AS n FROM answer WHERE question_id = %s AND value_text = 'Unsure' AND option_id IS NOT NULL",
        (question_id,),
    ).fetchone()
    assert landed == {"n": unsure}
