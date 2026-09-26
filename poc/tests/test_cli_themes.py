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
