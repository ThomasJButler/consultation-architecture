"""What the workbook promises (docs/02, step 12): the original columns and
one column per theme on a "Responses" sheet, a per-question summary, a
manifest, and every cell a text cell (docs/06, section 2.7;
THREAT_MODEL.md, row 7), on the fixture with factory tags on a signed-off
version (tests/rows.py, tests/pipeline.py).

Marked db: `write_workbook` reads Postgres. The pure prefix rule has its
own test, test_export_prefix.py, outside this mark (test_repo_rules.py).
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from openpyxl import load_workbook
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import DictRow

from consult import export, store
from consult.config import Settings
from consult.query import Filter, theme_table
from consult.store import EXPORT_ROLE, as_role
from tests.pipeline import signed_off_questions
from tests.rows import tag_answers_by_rule

pytestmark = pytest.mark.db


def _o_reason_key(text: str) -> str:
    # Both fragments name a real Oppose reason in REASONS
    # (scripts/make_fixture_data.py), so PARKING and SAFETY each get more
    # than one respondent, the same split test_query_db.py hand-counts.
    lowered = text.casefold()
    if "parking" in lowered:
        return "PARKING"
    if "junction" in lowered:
        return "SAFETY"
    return "OTHER"


def _o_safety_key(text: str) -> str:
    # "Proper lighting after dark along the towpath" is one of the eight
    # SAFETY fragments (scripts/make_fixture_data.py); the rest give OTHER.
    return "LIGHTING" if "lighting" in text.casefold() else "OTHER"


def test_the_workbook_has_text_cells_every_sheet_and_the_manifest(
    db: psycopg.Connection[DictRow], tmp_path: Path
) -> None:
    signed = signed_off_questions(db, ("o_reason", "o_safety"))
    tag_answers_by_rule(
        db, signed["o_reason"].version_id, signed["o_reason"].question_id, _o_reason_key
    )
    tag_answers_by_rule(
        db, signed["o_safety"].version_id, signed["o_safety"].question_id, _o_safety_key
    )

    path = tmp_path / "export.xlsx"
    result = export.write_workbook(db, signed["o_reason"].consultation_id, path)

    # respondents: the fixture's 240; answers: one row per respondent per
    # open question, blanks included (docs/04 section 6's export query
    # carries every row, not just the answered ones).
    assert result.respondents == 240
    assert result.answers == 480
    non_blank = db.execute(
        "SELECT count(*) AS n FROM answer WHERE question_id = ANY(%s) AND NOT is_blank",
        ([signed["o_reason"].question_id, signed["o_safety"].question_id],),
    ).fetchone()
    assert non_blank is not None
    assert result.tags == non_blank["n"]
    assert result.sheets == 4

    workbook = load_workbook(path)
    assert workbook.sheetnames == [
        "Responses",
        "o_reason summary",
        "o_safety summary",
        "Manifest",
    ]

    responses = workbook["Responses"]
    header = [cell.value for cell in next(responses.iter_rows(max_row=1))]
    assert header[:10] == [
        "respondent_ref",
        "email",
        "d_area",
        "d_commute",
        "d_age",
        "c_route",
        "c_modes",
        "c_safety",
        "o_reason",
        "o_safety",
    ]
    assert "o_reason: OTHER" in header
    assert "o_reason: PARKING" in header
    assert "o_reason: SAFETY" in header
    assert "o_safety: OTHER" in header
    assert "o_safety: LIGHTING" in header
    index = {name: i for i, name in enumerate(header)}

    rows = list(responses.iter_rows(min_row=2))
    assert len(rows) == 240
    by_ref = {row[index["respondent_ref"]].value: row for row in rows}

    # The fixture's one formula-trigger answer, CSV line 58, R-0057's
    # o_safety, reads back with its prefix and as a marked LIGHTING? no:
    # o_safety's real text there is "=1+1", which the safety-key rule
    # above tags OTHER since it names no lighting.
    r57 = by_ref["R-0057"]
    assert r57[index["o_safety"]].value == "'=1+1"
    assert r57[index["o_safety: OTHER"]].value == "1"
    assert r57[index["o_safety: LIGHTING"]].value is None

    # R-0001's o_safety cell is blank in the file (responses.csv) and
    # reads back as the file's own no-answer marker, not neutralised.
    r1 = by_ref["R-0001"]
    assert r1[index["o_safety"]].value == "-"

    # Every non-empty cell is a text cell everywhere in the workbook
    # (THREAT_MODEL.md, row 7); openpyxl itself would read a bare "=1+1"
    # back as a live formula otherwise (data_type "f"), which is exactly
    # what forcing it on write is for.
    for sheet in workbook.worksheets:
        for sheet_row in sheet.iter_rows():
            for cell in sheet_row:
                if cell.value not in (None, ""):
                    assert cell.data_type == "s", (sheet.title, cell.coordinate)

    # No identity value anywhere but its own column: email is unique per
    # respondent, so it can only be found where it's meant to be.
    email_col = index["email"]
    email = r57[email_col].value
    assert email == "respondent057@example.org"
    for row in rows:
        for col, cell in enumerate(row):
            if col != email_col:
                assert cell.value != email

    # The summary sheet's counts are query.theme_table's own counts under
    # the default filter, so the workbook and the dashboard can't disagree.
    question_department = db.execute(
        "SELECT department_id FROM question WHERE id = %s", (signed["o_reason"].question_id,)
    ).fetchone()
    assert question_department is not None
    expected = theme_table(
        db,
        signed["o_reason"].question_id,
        Filter(),
        department_id=question_department["department_id"],
    )
    summary = workbook["o_reason summary"]
    summary_rows = {row[0].value: row for row in summary.iter_rows(min_row=2)}
    assert set(summary_rows) == {c.key for c in expected.rows}
    for count in expected.rows:
        row = summary_rows[count.key]
        assert row[1].value == count.label
        assert row[2].value == str(count.respondents)
        assert row[3].value == str(expected.denominator)

    # The manifest names the consultation, the run and the export's own
    # facts, and nothing this schema doesn't hold (docs/02, step 12).
    manifest = {row[0].value: row[1].value for row in workbook["Manifest"].iter_rows(min_row=2)}
    assert manifest["consultation_id"] == str(signed["o_reason"].consultation_id)
    assert manifest["consultation_name"] == "Riverside cycle route"
    consultation = db.execute(
        "SELECT run_id FROM consultation WHERE id = %s", (signed["o_reason"].consultation_id,)
    ).fetchone()
    assert consultation is not None
    assert manifest["run_id"] == str(consultation["run_id"])
    assert manifest["retention_until"] == "-"
    duplicates = db.execute(
        """
        SELECT
          (SELECT count(*) FROM answer WHERE consultation_id = %(id)s
             AND duplicate_of_answer_id IS NOT NULL) AS answers,
          (SELECT count(*) FROM respondent WHERE consultation_id = %(id)s
             AND duplicate_of IS NOT NULL) AS respondents
        """,
        {"id": signed["o_reason"].consultation_id},
    ).fetchone()
    assert duplicates is not None
    assert manifest["duplicate_answers"] == str(duplicates["answers"])
    assert manifest["duplicate_respondents"] == str(duplicates["respondents"])

    per_question = list(workbook["Manifest"].iter_rows(min_row=9, values_only=True))
    by_column_ref = {row[0]: row for row in per_question}
    o_reason_row = by_column_ref["o_reason"]
    assert o_reason_row[1] == str(signed["o_reason"].version_id)
    assert o_reason_row[3] == "fake-model"  # find_themes_model_alias
    assert o_reason_row[4] == "fake-model"  # map_themes_model_alias
    o_reason_tags = db.execute(
        "SELECT count(*) AS n FROM answer_theme"
        " WHERE theme_set_version_id = %s AND retracted_at IS NULL",
        (signed["o_reason"].version_id,),
    ).fetchone()
    assert o_reason_tags is not None
    assert o_reason_row[5] == str(o_reason_tags["n"])
    assert o_reason_row[6] == "0"  # unprocessable_count: no mapping job ran


def test_export_reads_as_the_export_role(
    db: psycopg.Connection[DictRow], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signed = signed_off_questions(db, ("o_reason",))
    tag_answers_by_rule(
        db, signed["o_reason"].version_id, signed["o_reason"].question_id, _o_reason_key
    )

    # A stand-in for write_workbook's first read, as
    # test_cli_themes.py's test_run_job_works_as_the_pipeline_role stands
    # in for the worker's own call, wrapped rather than replaced so the
    # rest of the export still runs for real.
    seen: list[str] = []
    original = export._respondents

    def recording(
        conn: psycopg.Connection[DictRow], consultation_id: UUID
    ) -> list[export._Respondent]:
        row = conn.execute("SELECT current_user AS who").fetchone()
        assert row is not None
        seen.append(str(row["who"]))
        return original(conn, consultation_id)

    monkeypatch.setattr(export, "_respondents", recording)

    path = tmp_path / "export.xlsx"
    export.write_workbook(db, signed["o_reason"].consultation_id, path)

    # The reads ran as the export role, not the login user the connection
    # was made with (docs/06, section 2.4 as corrected).
    assert seen == [EXPORT_ROLE]

    # The role can read the vault: the identity column is in the workbook,
    # one value per respondent.
    workbook = load_workbook(path)
    responses = workbook["Responses"]
    header = [cell.value for cell in next(responses.iter_rows(max_row=1))]
    assert "email" in header
    email_col = header.index("email")
    emails = [str(row[email_col].value) for row in responses.iter_rows(min_row=2)]
    assert sum(1 for value in emails if "@" in value) == 240

    # And it holds no write grant it would need: an UPDATE under the role
    # is refused before it can touch a row (docs/06, section 2.8). The
    # savepoint keeps the connection usable for the RESET ROLE after.
    with as_role(db, EXPORT_ROLE), pytest.raises(InsufficientPrivilege), db.transaction():
        db.execute("UPDATE consultation SET name = name")


def test_export_reads_one_snapshot_despite_a_mid_export_retraction(
    db: psycopg.Connection[DictRow],
    db_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`write_workbook` reads the Responses sheet's theme marks and the
    manifest's tag_count from `export._tags`, then reads the summary
    sheet's counts again from `query.theme_table`, which resolves the
    signed-off version and the live tags afresh. A second connection
    retracts three PARKING tags and commits in between the two reads,
    from inside a wrapped `export._tags`: under READ COMMITTED each
    statement in `write_workbook`'s transaction takes its own snapshot
    (PostgreSQL 17 manual, 13.2.1), so the summary sees fewer tags than
    the sheet built from the read that ran first.
    """
    signed = signed_off_questions(db, ("o_reason",))
    tag_answers_by_rule(
        db, signed["o_reason"].version_id, signed["o_reason"].question_id, _o_reason_key
    )
    db.commit()

    # Three canonical (non-duplicate) PARKING answers: retracting these
    # changes query.theme_table's count, which the default Filter() scopes
    # to canonical rows only, unlike the Responses sheet's marks.
    targets = db.execute(
        "SELECT id FROM answer WHERE question_id = %s AND duplicate_of_answer_id IS NULL"
        " AND value_text ILIKE %s ORDER BY id LIMIT 3",
        (signed["o_reason"].question_id, "%parking%"),
    ).fetchall()
    target_ids = [row["id"] for row in targets]
    assert len(target_ids) == 3

    original_tags = export._tags
    retracted = False

    def retract_after_reading(
        conn: psycopg.Connection[DictRow], version_id: UUID | None
    ) -> dict[int, set[str]]:
        nonlocal retracted
        result = original_tags(conn, version_id)
        if not retracted and version_id == signed["o_reason"].version_id:
            retracted = True
            with store.connect(db_settings) as second:
                second.execute(
                    "UPDATE answer_theme SET retracted_at = now()"
                    " WHERE theme_set_version_id = %s AND answer_id = ANY(%s)",
                    (version_id, target_ids),
                )
                second.commit()
        return result

    monkeypatch.setattr(export, "_tags", retract_after_reading)

    path = tmp_path / "export.xlsx"
    export.write_workbook(db, signed["o_reason"].consultation_id, path)
    assert retracted

    workbook = load_workbook(path)
    responses = workbook["Responses"]
    header = [cell.value for cell in next(responses.iter_rows(max_row=1))]
    parking_col = header.index("o_reason: PARKING")
    responses_parking = sum(
        1 for row in responses.iter_rows(min_row=2) if row[parking_col].value == "1"
    )

    summary_rows = {row[0].value: row for row in workbook["o_reason summary"].iter_rows(min_row=2)}
    summary_parking = int(str(summary_rows["PARKING"][2].value))

    # A snapshot taken once for the whole gather can't see a retraction
    # committed by another connection after it started, so the summary
    # sheet's count agrees with the Responses sheet built from the same
    # snapshot, whatever the other connection commits in between.
    assert summary_parking == responses_parking

    manifest_rows = list(workbook["Manifest"].iter_rows(min_row=9, values_only=True))
    o_reason_row = {row[0]: row for row in manifest_rows}["o_reason"]
    responses_total_marks = sum(
        1
        for key in ("OTHER", "PARKING", "SAFETY")
        for row in responses.iter_rows(min_row=2)
        if row[header.index(f"o_reason: {key}")].value == "1"
    )
    assert o_reason_row[5] == str(responses_total_marks)  # tag_count
