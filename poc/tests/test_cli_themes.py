"""What the three review-side commands promise: `consult run-job` takes a
find_themes job from pending to succeeded with the model a fake, `consult
themes` shows a question's candidates as keys, labels, counts and answer
ids, and `consult sign-off` freezes them (docs/02, steps 6 to 8; ADR-003).

Every printed line carries ids, keys, labels and counts and never an
answer's text; the theme labels are the fake's, and the sign-off screen
is where a reviewer would read them.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow

from consult.cli import main
from consult.config import Settings

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RESPONSES = str(FIXTURES / "responses.csv")
DEFINITION = str(FIXTURES / "definition.xlsx")
ARGS = ["--name", "Riverside cycle route", "--department", "Department of Fictional Affairs"]
ANSWER_FRAGMENTS = ("towpath", "Mill Lane", "school run", "example.org", "R-0001")


def ingested(db: psycopg.Connection[DictRow], settings: Settings) -> dict[str, UUID]:
    """The fixtures ingested through the command; the find_themes job id per
    open question, read back from the rows."""
    assert main(["ingest", RESPONSES, "--definition", DEFINITION, *ARGS], settings=settings) == 0
    rows = db.execute(
        """
        SELECT q.column_ref, j.id FROM job j JOIN question q ON q.id = j.question_id
         WHERE j.kind = 'find_themes'
        """
    ).fetchall()
    return {str(r["column_ref"]): UUID(str(r["id"])) for r in rows}


def test_the_run_job_command_runs_a_find_themes_job_with_the_fake(
    db: psycopg.Connection[DictRow], db_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs = ingested(db, db_settings)
    capsys.readouterr()

    code = main(
        ["run-job", str(jobs["o_reason"]), "--worker", "w1", "--model", "fake"],
        settings=db_settings,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert f"job {jobs['o_reason']}" in out and "themes_ready" in out
    assert re.search(r"\b4 generate\b", out) and re.search(r"\b1 condense\b", out)
    assert re.search(r"shortlist 3\b", out) and re.search(r"longlist 12\b", out)
    assert "consultation processing" in out
    for fragment in ANSWER_FRAGMENTS:
        assert fragment not in out
    state = db.execute(
        """
        SELECT j.status AS job, j.attempts, j.model_alias, q.status AS question,
               (SELECT count(*) FROM theme t JOIN theme_set_version v ON v.id = t.theme_set_version_id
                 WHERE v.question_id = q.id AND NOT t.is_longlist) AS shortlist,
               (SELECT count(*) FROM job_batch WHERE job_id = j.id) AS checkpoints
          FROM job j JOIN question q ON q.id = j.question_id WHERE j.id = %s
        """,
        (jobs["o_reason"],),
    ).fetchone()
    assert state is not None
    assert (state["job"], state["attempts"], state["model_alias"], state["question"]) == (
        "succeeded",
        1,
        "fake",
        "themes_ready",
    )
    assert state["shortlist"] == 3 and state["checkpoints"] >= 6

    # A second delivery of the same job can't claim it and says so.
    assert (
        main(
            ["run-job", str(jobs["o_reason"]), "--worker", "w2", "--model", "fake"],
            settings=db_settings,
        )
        == 1
    )
    assert "not claimable" in capsys.readouterr().out
    # The other question completes the consultation.
    assert (
        main(
            ["run-job", str(jobs["o_safety"]), "--worker", "w1", "--model", "fake"],
            settings=db_settings,
        )
        == 0
    )
    assert "consultation awaiting_review" in capsys.readouterr().out


def test_the_themes_command_prints_keys_labels_counts_and_ids(
    db: psycopg.Connection[DictRow], db_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs = ingested(db, db_settings)
    assert (
        main(
            ["run-job", str(jobs["o_reason"]), "--worker", "w1", "--model", "fake"],
            settings=db_settings,
        )
        == 0
    )
    question = db.execute(
        "SELECT question_id FROM job WHERE id = %s", (jobs["o_reason"],)
    ).fetchone()
    assert question is not None
    question_id = question["question_id"]
    counted = db.execute(
        """
        SELECT t.key, t.preview_count FROM theme t JOIN theme_set_version v ON v.id = t.theme_set_version_id
         WHERE v.question_id = %s AND NOT t.is_longlist ORDER BY t.key
        """,
        (question_id,),
    ).fetchall()
    capsys.readouterr()

    code = main(["themes", str(question_id)], settings=db_settings)

    out = capsys.readouterr().out
    assert code == 0
    assert f"question {question_id}: version 1 (candidate, edit 0)" in out
    # The shortlist: key, label, count and the example answer ids the
    # reviewer would open (docs/02, step 8's screen), then the longlist with
    # what each folded into.
    for row in counted:
        assert re.search(rf"^\s+{row['key']}\s+\S.*\bcount {row['preview_count']}\b", out, re.M)
    access = re.search(r"^\s+ACCESS\b.*\bexamples (\d+), (\d+), (\d+)", out, re.M)
    assert access is not None
    example_ids = {int(n) for n in access.groups()}
    known = db.execute("SELECT id FROM answer WHERE question_id = %s", (question_id,)).fetchall()
    assert example_ids <= {int(r["id"]) for r in known}
    assert "longlist (12)" in out and re.search(r"ACCESS_\d+ > ACCESS", out)
    for fragment in ANSWER_FRAGMENTS:
        assert fragment not in out

    # A question with no theme set yet, and one that doesn't exist.
    other = db.execute("SELECT question_id FROM job WHERE id = %s", (jobs["o_safety"],)).fetchone()
    assert other is not None
    assert main(["themes", str(other["question_id"])], settings=db_settings) == 1
    assert "no theme set" in capsys.readouterr().out
    assert main(["themes", "00000000-0000-0000-0000-000000000000"], settings=db_settings) == 1


def test_the_sign_off_command_freezes_v2_and_refuses_a_second(
    db: psycopg.Connection[DictRow], db_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs = ingested(db, db_settings)
    for column_ref in ("o_reason", "o_safety"):
        assert (
            main(
                ["run-job", str(jobs[column_ref]), "--worker", "w1", "--model", "fake"],
                settings=db_settings,
            )
            == 0
        )
    questions = {
        ref: db.execute("SELECT question_id FROM job WHERE id = %s", (job_id,)).fetchone()
        for ref, job_id in jobs.items()
    }
    reason = str(questions["o_reason"]["question_id"]) if questions["o_reason"] else ""
    safety = str(questions["o_safety"]["question_id"]) if questions["o_safety"] else ""
    reviewer = "11111111-2222-3333-4444-555555555555"
    capsys.readouterr()

    code = main(
        ["sign-off", reason, "--reviewer", reviewer, "--expect-version", "0"], settings=db_settings
    )

    out = capsys.readouterr().out
    assert code == 0
    assert f"question {reason} signed off" in out and "map_themes job" in out and "pending" in out
    frozen = db.execute(
        """
        SELECT v.version_no, v.status, v.signed_off_by,
               (SELECT string_agg(t.key, ',' ORDER BY t.key) FROM theme t
                 WHERE t.theme_set_version_id = v.id AND NOT t.is_longlist) AS shortlist,
               (SELECT status FROM question WHERE id = v.question_id) AS question,
               (SELECT count(*) FROM job WHERE question_id = v.question_id AND kind = 'map_themes'
                   AND status = 'pending') AS map_jobs
          FROM theme_set_version v WHERE v.question_id = %s ORDER BY v.version_no
        """,
        (reason,),
    ).fetchall()
    assert [(f["version_no"], f["status"]) for f in frozen] == [
        (1, "superseded"),
        (2, "signed_off"),
    ]
    assert str(frozen[1]["signed_off_by"]) == reviewer
    assert frozen[1]["shortlist"] == "ACCESS,NO_REASON,OTHER,PARKING,SAFETY"
    assert (frozen[1]["question"], frozen[1]["map_jobs"]) == ("signed_off", 1)

    # A second sign-off is refused with nothing changed (ADR-003: the guard is the mutex).
    assert (
        main(
            ["sign-off", reason, "--reviewer", reviewer, "--expect-version", "0"],
            settings=db_settings,
        )
        == 1
    )
    assert "refused" in capsys.readouterr().out
    versions = db.execute(
        "SELECT count(*) AS n FROM theme_set_version WHERE question_id = %s", (reason,)
    ).fetchone()
    assert versions == {"n": 2}
    # A reviewer looking at the wrong edit gets a conflict, and the question stays put.
    assert (
        main(
            ["sign-off", safety, "--reviewer", reviewer, "--expect-version", "3"],
            settings=db_settings,
        )
        == 1
    )
    assert "conflict" in capsys.readouterr().out
    state = db.execute(
        "SELECT status, (SELECT count(*) FROM theme_set_version WHERE question_id = %(q)s) AS versions FROM question WHERE id = %(q)s",
        {"q": safety},
    ).fetchone()
    assert state == {"status": "themes_ready", "versions": 1}
