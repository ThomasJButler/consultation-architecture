"""What the map_themes job promises (docs/02, step 9).

Driven stage by stage with the model a fake, from a question already
signed off: batches of ten distinct answers against v2's frozen keys, tags
under the fence, duplicates carrying their canonical's tags. Every
expectation is read from the fixture CSV or the fakes' script, not from
the code under test.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import DictRow

from consult.jobs import LeaseLostError, claim
from consult.mapping import MAP_BATCH_SIZE, assign, load_map_job, plan
from tests.fakes import RecordingLLM
from tests.pipeline import signed_off_fixture
from tests.test_themes import distinct_reasons

pytestmark = pytest.mark.db


def test_mapping_batches_ten_shuffled_answers_and_tags_under_the_fence(
    db: psycopg.Connection[DictRow],
) -> None:
    signed = signed_off_fixture(db)
    job = load_map_job(db, signed.job_id)
    assert (job.model_alias, job.seed, job.version_id) == ("fake-model", 7, signed.version_id)
    assert job.question_id == signed.question_id

    planned = plan(db, job)
    expected = distinct_reasons()

    # Every distinct answer once, in batches of at most ten, and the same
    # seed gives the same plan twice (docs/02, step 9).
    assert MAP_BATCH_SIZE == 10
    assert all(len(batch.answers) <= MAP_BATCH_SIZE for batch in planned)
    texts = sorted(answer.text for batch in planned for answer in batch.answers)
    assert texts == sorted(text for _no, text, _related in expected)
    assert plan(db, job) == planned

    lease = claim(db, signed.job_id, "worker-1")
    assert lease is not None
    llm = RecordingLLM()

    ran = assign(db, llm, lease, job, planned)

    assert ran == len(planned)
    keys = [
        str(r["key"])
        for r in db.execute(
            """
            SELECT key FROM theme
             WHERE theme_set_version_id = %s AND NOT is_longlist ORDER BY key
            """,
            (job.version_id,),
        ).fetchall()
    ]
    # The five keys sign-off froze on the fixture: the three the fake's
    # condensation folded plus the two fallbacks (transitions.FALLBACK_THEMES).
    assert keys == ["ACCESS", "NO_REASON", "OTHER", "PARKING", "SAFETY"]
    for prompt, batch in zip(llm.prompts, planned, strict=True):
        assert prompt.theme_keys == tuple(keys)
        assert prompt.answer_ids == tuple(answer.id for answer in batch.answers)

    # One tag per distinct answer per key the fake returned (the first
    # enum key), with source, job and the checkpoint's batch.
    tags = db.execute(
        """
        SELECT a.id AS answer_id, t.source, t.job_id, t.batch_no
          FROM answer a JOIN answer_theme t ON t.answer_id = a.id
         WHERE a.question_id = %s AND a.duplicate_of_answer_id IS NULL
        """,
        (job.question_id,),
    ).fetchall()
    assert len(tags) == len(expected)
    assert {t["source"] for t in tags} == {"ai"}
    assert {t["job_id"] for t in tags} == {signed.job_id}

    checkpoints = db.execute(
        "SELECT batch_no, stage, answer_ids FROM job_batch WHERE job_id = %s ORDER BY batch_no",
        (signed.job_id,),
    ).fetchall()
    assert [c["batch_no"] for c in checkpoints] == list(range(1, len(planned) + 1))
    assert {c["stage"] for c in checkpoints} == {"map"}
    assert [tuple(c["answer_ids"]) for c in checkpoints] == [
        tuple(a.id for a in batch.answers) for batch in planned
    ]
    by_batch = {c["batch_no"]: set(c["answer_ids"]) for c in checkpoints}
    assert all(t["answer_id"] in by_batch[t["batch_no"]] for t in tags)

    # A stale fence writes nothing (docs/02, step 5; ADR-002).
    tags_before = db.execute("SELECT count(*) AS n FROM answer_theme").fetchone()
    batches_before = db.execute(
        "SELECT count(*) AS n FROM job_batch WHERE job_id = %s", (signed.job_id,)
    ).fetchone()
    db.execute(
        "UPDATE job SET heartbeat_at = now() - interval '11 minutes' WHERE id = %s",
        (signed.job_id,),
    )
    assert claim(db, signed.job_id, "worker-2") is not None

    with pytest.raises(LeaseLostError):
        assign(db, RecordingLLM(), lease, job, planned)

    assert db.execute("SELECT count(*) AS n FROM answer_theme").fetchone() == tags_before
    assert (
        db.execute(
            "SELECT count(*) AS n FROM job_batch WHERE job_id = %s", (signed.job_id,)
        ).fetchone()
        == batches_before
    )
