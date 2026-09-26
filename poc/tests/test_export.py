"""What the workbook promises (docs/02, step 12): the original columns and
one column per theme on a "Responses" sheet, a per-question summary, a
manifest, and every cell a text cell (docs/06, section 2.7;
THREAT_MODEL.md, row 7), on the fixture with factory tags on a signed-off
version (tests/rows.py, tests/pipeline.py).

Marked db: `write_workbook` reads Postgres. The pure prefix rule has its
own test, test_export_prefix.py, outside this mark (test_repo_rules.py).
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from defusedxml import ElementTree
from openpyxl import load_workbook
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import DictRow

from consult import export, store
from consult.config import Settings
from consult.query import Filter, theme_table
from consult.store import EXPORT_ROLE, as_role
from tests.pipeline import signed_off_questions
from tests.rows import make_job_batch, tag_answers_by_rule

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
    """`write_workbook` reads the tags for the Responses sheet and the
    manifest from `export._tags`, then reads them again, independently,
    for the summary sheet, via `query.theme_table`. A second connection
    retracts three PARKING tags and commits in between the two reads,
    from inside a wrapped `export._tags`. Under READ COMMITTED each
    statement in `write_workbook`'s transaction takes its own snapshot
    (PostgreSQL 17 manual, 13.2.1), so `theme_table`'s read, running
    later in the same transaction, sees the retraction the first read
    didn't; a snapshot taken once for the whole gather can't. `baseline`
    is `theme_table`'s own count before the retraction: what the summary
    sheet has to still read afterwards if the export holds to one
    snapshot, since the retraction lands after `baseline` is read too.
    """
    signed = signed_off_questions(db, ("o_reason",))
    tag_answers_by_rule(
        db, signed["o_reason"].version_id, signed["o_reason"].question_id, _o_reason_key
    )
    db.commit()

    department = db.execute(
        "SELECT department_id FROM question WHERE id = %s", (signed["o_reason"].question_id,)
    ).fetchone()
    assert department is not None
    baseline = {
        row.key: row.respondents
        for row in theme_table(
            db,
            signed["o_reason"].question_id,
            Filter(),
            department_id=department["department_id"],
        ).rows
    }["PARKING"]

    # Three canonical (non-duplicate) PARKING answers: retracting these
    # is what moves query.theme_table's canonical-scope count, unlike
    # retracting one of the campaign proforma's duplicate rows would.
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
    summary_rows = {row[0].value: row for row in workbook["o_reason summary"].iter_rows(min_row=2)}
    summary_parking = int(str(summary_rows["PARKING"][2].value))

    # One snapshot for the whole gather: the summary sheet still reads
    # the count from before the retraction, whatever a second connection
    # commits once the export is already under way.
    assert summary_parking == baseline

    # The Responses sheet and the manifest's tag_count agree with each
    # other regardless, since both come from the same in-memory read;
    # this is the pair the isolation level doesn't have to fix.
    responses = workbook["Responses"]
    header = [cell.value for cell in next(responses.iter_rows(max_row=1))]
    responses_total_marks = sum(
        1
        for key in ("OTHER", "PARKING", "SAFETY")
        for row in responses.iter_rows(min_row=2)
        if row[header.index(f"o_reason: {key}")].value == "1"
    )
    manifest_rows = list(workbook["Manifest"].iter_rows(min_row=9, values_only=True))
    o_reason_row = {row[0]: row for row in manifest_rows}["o_reason"]
    assert o_reason_row[5] == str(responses_total_marks)  # tag_count


def test_a_summary_sheet_title_is_always_valid(
    db: psycopg.Connection[DictRow], tmp_path: Path
) -> None:
    """`question.column_ref` is a free-text header from the definition
    workbook (docs/00), not a sheet-safe string. openpyxl's own title
    setter raises ValueError on any of `? / \\ * [ ] :`
    (`openpyxl.workbook.child.INVALID_TITLE_REGEX`), and Excel's own
    sheet-title limit is 31 characters, which " summary" alone leaves
    room for 23 of."""
    signed = signed_off_questions(db, ("o_reason",))
    tag_answers_by_rule(
        db, signed["o_reason"].version_id, signed["o_reason"].question_id, _o_reason_key
    )

    dirty_ref = ("Why/expand? " * 4)[:40]
    assert len(dirty_ref) == 40
    assert "?" in dirty_ref
    assert "/" in dirty_ref
    db.execute(
        "UPDATE question SET column_ref = %s WHERE id = %s",
        (dirty_ref, signed["o_reason"].question_id),
    )

    path = tmp_path / "export.xlsx"
    export.write_workbook(db, signed["o_reason"].consultation_id, path)

    workbook = load_workbook(path)
    # o_safety is configured but never signed off here, and still gets
    # its own summary sheet (query.theme_table over an empty version):
    # only o_reason's title is dirty.
    summary_titles = [name for name in workbook.sheetnames if name not in ("Responses", "Manifest")]
    assert len(summary_titles) == 2
    assert "o_safety summary" in summary_titles
    title = next(name for name in summary_titles if name != "o_safety summary")
    assert len(title) <= 31
    assert "?" not in title
    assert "/" not in title
    assert workbook[title]["A1"].value == "key"


def test_unprocessable_counts_the_versions_own_map_job(
    db: psycopg.Connection[DictRow], tmp_path: Path
) -> None:
    """A reopen and a second sign-off puts a new map_themes job under a
    new run, which re-maps every answer (docs/02, step 9). The manifest's
    unprocessable_count has to name the version's own run, not every
    map_themes job the question has ever had: an answer refused by both
    an earlier run and the run that tagged this version is one refused
    answer, not two."""
    signed = signed_off_questions(db, ("o_reason",))
    shared_answer = db.execute(
        "SELECT id FROM answer WHERE question_id = %s AND NOT is_blank ORDER BY id LIMIT 1",
        (signed["o_reason"].question_id,),
    ).fetchone()
    assert shared_answer is not None
    shared_answer_id = shared_answer["id"]

    # sign_off's own map_themes job (docs/02, step 9): refused the shared
    # answer in an earlier run.
    make_job_batch(db, signed["o_reason"].job_id, [shared_answer_id], status="unprocessable")

    # A reopen's new run (docs/02, step 9): a fresh run_id, since
    # job_one_per_run scopes one job per (consultation, question, kind,
    # run). Created after the first job, and the alias query in
    # _manifest_question already picks this one as "the" map_themes job
    # by created_at.
    second_job = db.execute(
        """
        INSERT INTO job (department_id, consultation_id, question_id, kind, run_id, status,
                         created_at)
        SELECT department_id, id, %s, 'map_themes', %s, 'queued', now() + interval '1 minute'
          FROM consultation WHERE id = %s
        RETURNING id
        """,
        (signed["o_reason"].question_id, uuid4(), signed["o_reason"].consultation_id),
    ).fetchone()
    assert second_job is not None
    make_job_batch(db, second_job["id"], [shared_answer_id], status="unprocessable")

    path = tmp_path / "export.xlsx"
    export.write_workbook(db, signed["o_reason"].consultation_id, path)

    workbook = load_workbook(path)
    manifest_rows = list(workbook["Manifest"].iter_rows(min_row=9, values_only=True))
    o_reason_row = {row[0]: row for row in manifest_rows}["o_reason"]
    assert o_reason_row[6] == "1"  # unprocessable_count


def test_a_control_character_never_rides_the_exports_error(
    db: psycopg.Connection[DictRow], tmp_path: Path
) -> None:
    """openpyxl 3.1.5 (cell.py lines 164-165) raises IllegalCharacterError
    with the whole value in its own message for any C0 control character
    other than tab, newline or carriage return, and nothing here used to
    catch it: THREAT_MODEL.md section 2, line 2 forbids an open answer or
    a vault value at any level, an exception message included. Two
    carriers, dirtied by hand: an open answer's value_text, and a vault
    identity value (store.identity_columns names the table and column).
    """
    signed = signed_off_questions(db, ("o_reason",))
    tag_answers_by_rule(
        db, signed["o_reason"].version_id, signed["o_reason"].question_id, _o_reason_key
    )

    target = db.execute(
        "SELECT id, respondent_id, value_text FROM answer"
        " WHERE question_id = %s AND NOT is_blank ORDER BY id LIMIT 1",
        (signed["o_reason"].question_id,),
    ).fetchone()
    assert target is not None
    original_answer_text = target["value_text"]
    assert isinstance(original_answer_text, str)
    assert len(original_answer_text) >= 10
    mid = len(original_answer_text) // 2
    dirty_answer = original_answer_text[:mid] + "\x0b" + original_answer_text[mid:]
    db.execute("UPDATE answer SET value_text = %s WHERE id = %s", (dirty_answer, target["id"]))

    identity = db.execute(
        "SELECT column_ref, value_text FROM vault.respondent_identity WHERE respondent_id = %s",
        (target["respondent_id"],),
    ).fetchone()
    assert identity is not None
    original_identity_text = identity["value_text"]
    assert isinstance(original_identity_text, str)
    assert len(original_identity_text) >= 4
    imid = len(original_identity_text) // 2
    dirty_identity = original_identity_text[:imid] + "\x1b" + original_identity_text[imid:]
    db.execute(
        "UPDATE vault.respondent_identity SET value_text = %s"
        " WHERE respondent_id = %s AND column_ref = %s",
        (dirty_identity, target["respondent_id"], identity["column_ref"]),
    )

    respondent = db.execute(
        "SELECT external_id FROM respondent WHERE id = %s", (target["respondent_id"],)
    ).fetchone()
    assert respondent is not None

    path = tmp_path / "export.xlsx"
    export.write_workbook(db, signed["o_reason"].consultation_id, path)

    workbook = load_workbook(path)
    responses = workbook["Responses"]
    header = [cell.value for cell in next(responses.iter_rows(max_row=1))]
    index = {name: i for i, name in enumerate(header)}
    row = next(
        r
        for r in responses.iter_rows(min_row=2)
        if r[index["respondent_ref"]].value == respondent["external_id"]
    )

    answer_cell = row[index["o_reason"]].value
    assert isinstance(answer_cell, str)
    assert "\x0b" not in answer_cell
    assert "\\x0b" in answer_cell

    identity_cell = row[index[identity["column_ref"]]].value
    assert isinstance(identity_cell, str)
    assert "\x1b" not in identity_cell
    assert "\\x1b" in identity_cell


def test_a_noncharacter_never_breaks_the_workbook(
    db: psycopg.Connection[DictRow], tmp_path: Path
) -> None:
    """XML 1.0's Char production (section 2.2) admits tab, LF, CR,
    U+0020 to U+D7FF, U+E000 to U+FFFD and U+10000 up, so U+FFFE and
    U+FFFF are no more legal in a sheet part than a C0 control is.
    openpyxl 3.1.5 without lxml (openpyxl.xml.LXML is False here) hands
    them to ElementTree, which writes them raw, and the save succeeds
    with a sheet no XML parser will read. Postgres stores both in UTF-8
    text, so an open answer or a vault identity value can carry one. The
    same two carriers as the control-character test above, dirtied by
    hand; every worksheet part is parsed straight out of the zip, since
    Excel refuses the file where openpyxl's own reader might not.
    """
    signed = signed_off_questions(db, ("o_reason",))
    target = db.execute(
        "SELECT id, respondent_id, value_text FROM answer"
        " WHERE question_id = %s AND NOT is_blank ORDER BY id LIMIT 1",
        (signed["o_reason"].question_id,),
    ).fetchone()
    assert target is not None
    answer_text = target["value_text"]
    assert isinstance(answer_text, str)
    db.execute(
        "UPDATE answer SET value_text = %s WHERE id = %s",
        (answer_text + "\uffff", target["id"]),
    )

    identity = db.execute(
        "SELECT column_ref, value_text FROM vault.respondent_identity WHERE respondent_id = %s",
        (target["respondent_id"],),
    ).fetchone()
    assert identity is not None
    identity_text = identity["value_text"]
    assert isinstance(identity_text, str)
    db.execute(
        "UPDATE vault.respondent_identity SET value_text = %s"
        " WHERE respondent_id = %s AND column_ref = %s",
        ("\ufffe" + identity_text, target["respondent_id"], identity["column_ref"]),
    )
    respondent = db.execute(
        "SELECT external_id FROM respondent WHERE id = %s", (target["respondent_id"],)
    ).fetchone()
    assert respondent is not None
    db.commit()

    path = tmp_path / "export.xlsx"
    export.write_workbook(db, signed["o_reason"].consultation_id, path)

    with zipfile.ZipFile(path) as archive:
        parts = [name for name in archive.namelist() if name.startswith("xl/worksheets/")]
        assert len(parts) == 4
        for name in parts:
            ElementTree.fromstring(archive.read(name))

    workbook = load_workbook(path)
    responses = workbook["Responses"]
    header = [cell.value for cell in next(responses.iter_rows(max_row=1))]
    index = {name: i for i, name in enumerate(header)}
    row = next(
        r
        for r in responses.iter_rows(min_row=2)
        if r[index["respondent_ref"]].value == respondent["external_id"]
    )
    # The visible form the control-character escape already uses.
    assert row[index["o_reason"]].value == answer_text + "\\uffff"
    assert row[index[identity["column_ref"]]].value == "\\ufffe" + identity_text


def test_the_responses_sheet_has_a_column_per_shortlist_theme_only(
    db: psycopg.Connection[DictRow], tmp_path: Path
) -> None:
    """sign_off copies every theme of the candidate into the signed-off
    version, the longlist too, for lineage (transitions.sign_off), while
    mapping offers the model the shortlist and its two fallbacks and
    nothing else (mapping._shortlist reads NOT is_longlist). A column for
    a longlist key could never hold a mark, so docs/02 step 12's "one
    column per theme" is one per theme an answer can be tagged with."""
    signed = signed_off_questions(db, ("o_reason",))
    themes = db.execute(
        "SELECT key, is_longlist FROM theme WHERE theme_set_version_id = %s ORDER BY key",
        (signed["o_reason"].version_id,),
    ).fetchall()
    shortlist = [row["key"] for row in themes if not row["is_longlist"]]
    longlist = {row["key"] for row in themes if row["is_longlist"]}
    # The fake's condensation leaves candidates on the longlist, so the
    # fixture's signed-off version has some to leave out.
    assert longlist
    db.commit()

    path = tmp_path / "export.xlsx"
    export.write_workbook(db, signed["o_reason"].consultation_id, path)

    workbook = load_workbook(path)
    header = [str(cell.value) for cell in next(workbook["Responses"].iter_rows(max_row=1))]
    columns = [name.removeprefix("o_reason: ") for name in header if name.startswith("o_reason: ")]
    assert columns == shortlist
    assert not longlist & set(columns)


def test_a_cell_at_the_cap_keeps_a_visible_cut(
    db: psycopg.Connection[DictRow], tmp_path: Path
) -> None:
    """openpyxl 3.1.5's check_string cuts every string to 32,767
    characters, Excel's own cell limit, without a word (cell.py line 163),
    and the input's cell cap is the same number (CONSULT_MAX_CELL_CHARS in
    .env.example), so an answer at the cap that `neutralise` prefixes, or
    that an escape lengthens, loses its tail silently. The cut has to show
    where it is and the manifest has to count it. Hand count: 1 + 32,765 +
    1 is the cap, 32,768 once prefixed, cut to 32,764 and "..." appended;
    one character shorter, it fits exactly once prefixed."""
    signed = signed_off_questions(db, ("o_reason",))
    targets = db.execute(
        "SELECT a.id, r.external_id FROM answer a JOIN respondent r ON r.id = a.respondent_id"
        " WHERE a.question_id = %s AND NOT a.is_blank ORDER BY a.id LIMIT 2",
        (signed["o_reason"].question_id,),
    ).fetchall()
    assert len(targets) == 2
    at_cap = "=" + "a" * 32765 + "Z"
    under = "=" + "a" * 32764 + "Z"
    assert (len(at_cap), len(under)) == (32767, 32766)
    for target, text in zip(targets, (at_cap, under), strict=True):
        db.execute("UPDATE answer SET value_text = %s WHERE id = %s", (text, target["id"]))
    db.commit()

    path = tmp_path / "export.xlsx"
    export.write_workbook(db, signed["o_reason"].consultation_id, path)

    workbook = load_workbook(path)
    responses = workbook["Responses"]
    header = [cell.value for cell in next(responses.iter_rows(max_row=1))]
    index = {name: i for i, name in enumerate(header)}
    by_ref = {row[index["respondent_ref"]].value: row for row in responses.iter_rows(min_row=2)}
    cut = by_ref[targets[0]["external_id"]][index["o_reason"]].value
    assert cut == "'=" + "a" * 32762 + "..."
    assert by_ref[targets[1]["external_id"]][index["o_reason"]].value == "'" + under

    manifest = {row[0].value: row[1].value for row in workbook["Manifest"].iter_rows(min_row=2)}
    assert manifest["truncated_cells"] == "1"
