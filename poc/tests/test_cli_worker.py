"""What `consult worker --once` and `consult reconcile` promise, run over
the fixtures through the commands (docs/02, section 5 and step 5; the
objective in plans/PR-08-poc-mapping-worker.md section 1).

`consult ingest`, then `consult worker --once` twice, takes both open
questions to `themes_ready` and the consultation to `awaiting_review` with
one `themes_ready` outbox row. Two sign-offs and `consult worker --once`
twice more take both questions to `complete`, tag every distinct answer
once against v2 with duplicates carrying the same tags, and leave the
consultation `ready` with one `analysis_ready` row. `consult reconcile`
then changes no job row and no question row: the outbox is the one table
it moves, relaying both rows to `sent` (plans/final-run.md, section 10,
"only the outbox rows move to sent"). Every printed line carries ids,
kinds, statuses, counts and codes and never an answer's text.
"""

from __future__ import annotations

import re
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow

from consult.cli import main
from consult.config import Settings
from tests.test_cli_themes import ANSWER_FRAGMENTS, ingested

pytestmark = pytest.mark.db


def test_the_worker_and_reconcile_commands_run_the_fixtures_to_ready(
    db: psycopg.Connection[DictRow], db_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    ingested(db, db_settings)
    question_ids = [
        row["id"]
        for row in db.execute(
            "SELECT id FROM question WHERE kind = 'open' ORDER BY ordinal"
        ).fetchall()
    ]
    assert len(question_ids) == 2
    capsys.readouterr()

    for _ in range(2):
        assert main(["worker", "--once", "--worker", "w1"], settings=db_settings) == 0
    out = capsys.readouterr().out
    for fragment in ANSWER_FRAGMENTS:
        assert fragment not in out
    assert out.count("find_themes") == 2 and out.count("succeeded") == 2
    assert re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", out)

    statuses = db.execute("SELECT status FROM question WHERE kind = 'open'").fetchall()
    assert {row["status"] for row in statuses} == {"themes_ready"}
    consultation = db.execute("SELECT status FROM consultation").fetchone()
    assert consultation == {"status": "awaiting_review"}
    outbox = db.execute("SELECT kind, status FROM notification_outbox").fetchall()
    assert outbox == [{"kind": "themes_ready", "status": "pending"}]

    reviewer = str(uuid4())
    for question_id in question_ids:
        assert (
            main(
                ["sign-off", str(question_id), "--reviewer", reviewer, "--expect-version", "0"],
                settings=db_settings,
            )
            == 0
        )
    capsys.readouterr()

    for _ in range(2):
        assert main(["worker", "--once", "--worker", "w1"], settings=db_settings) == 0
    out = capsys.readouterr().out
    for fragment in ANSWER_FRAGMENTS:
        assert fragment not in out
    assert out.count("map_themes") == 2 and out.count("succeeded") == 2

    statuses = db.execute("SELECT status FROM question WHERE kind = 'open'").fetchall()
    assert {row["status"] for row in statuses} == {"complete"}
    consultation = db.execute("SELECT status FROM consultation").fetchone()
    assert consultation == {"status": "ready"}
    outbox = db.execute("SELECT kind, status FROM notification_outbox ORDER BY kind").fetchall()
    assert outbox == [
        {"kind": "analysis_ready", "status": "pending"},
        {"kind": "themes_ready", "status": "pending"},
    ]

    # Every distinct, non-blank answer tagged once against v2, and every
    # exact duplicate carrying its canonical's tag (docs/02, step 9).
    tags = db.execute(
        """
        SELECT t.answer_id, t.theme_id, v.version_no
          FROM answer_theme t
          JOIN theme_set_version v ON v.id = t.theme_set_version_id
         WHERE t.answer_id IN (SELECT id FROM answer WHERE question_id = ANY(%s))
        """,
        (question_ids,),
    ).fetchall()
    assert tags
    assert {row["version_no"] for row in tags} == {2}
    by_answer: dict[int, set[UUID]] = {}
    for row in tags:
        by_answer.setdefault(row["answer_id"], set()).add(row["theme_id"])
    assert all(len(keys) == 1 for keys in by_answer.values())
    canonical = db.execute(
        """
        SELECT id FROM answer
         WHERE question_id = ANY(%s) AND duplicate_of_answer_id IS NULL AND NOT is_blank
        """,
        (question_ids,),
    ).fetchall()
    assert {row["id"] for row in canonical} == set(by_answer)
    duplicates = db.execute(
        """
        SELECT id, duplicate_of_answer_id FROM answer
         WHERE question_id = ANY(%s) AND duplicate_of_answer_id IS NOT NULL
        """,
        (question_ids,),
    ).fetchall()
    assert duplicates  # the campaign proforma test_ingest.py flags
    for row in duplicates:
        assert by_answer[row["id"]] == by_answer[row["duplicate_of_answer_id"]]

    # consult reconcile changes no job row and no question row: the outbox
    # is the one table it moves (plans/final-run.md, section 10).
    jobs_before = db.execute("SELECT * FROM job ORDER BY id").fetchall()
    questions_before = db.execute("SELECT * FROM question ORDER BY id").fetchall()
    capsys.readouterr()

    assert main(["reconcile"], settings=db_settings) == 0

    out = capsys.readouterr().out
    for fragment in ANSWER_FRAGMENTS:
        assert fragment not in out
    assert "0 dispatched" in out and "0 resent" in out and "0 failed" in out
    assert "0 retried" in out and "0 advanced" in out and "2 relayed" in out
    assert db.execute("SELECT * FROM job ORDER BY id").fetchall() == jobs_before
    assert db.execute("SELECT * FROM question ORDER BY id").fetchall() == questions_before
    outbox = db.execute("SELECT kind, status FROM notification_outbox ORDER BY kind").fetchall()
    assert outbox == [
        {"kind": "analysis_ready", "status": "sent"},
        {"kind": "themes_ready", "status": "sent"},
    ]

    # Nothing runnable: prints so, exits 0, and logs no answer text either.
    capsys.readouterr()
    assert main(["worker", "--once"], settings=db_settings) == 0
    assert "nothing to run" in capsys.readouterr().out
