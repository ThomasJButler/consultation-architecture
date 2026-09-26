"""What the map_themes job promises (docs/02, step 9).

Driven stage by stage with the model a fake, from a question already
signed off: batches of ten distinct answers against v2's frozen keys, tags
under the fence, duplicates carrying their canonical's tags. Every
expectation is read from the fixture CSV or the fakes' script, not from
the code under test.
"""

from __future__ import annotations

from collections import Counter
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow

from consult.jobs import LeaseLostError, claim
from consult.mapping import MAP_BATCH_SIZE, assign, load_map_job, plan
from consult.transitions import finish_map_themes, start_map_themes
from tests.fakes import FakeLLM, Fault, RecordingLLM
from tests.pipeline import NOT_ANSWERED, fixture_rows, signed_off_fixture
from tests.test_ingest import normalised
from tests.test_themes import WorkerCrashError, crash_after, distinct_reasons

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


def _proforma() -> tuple[str, int]:
    """The most-repeated o_reason text and its copy count: the campaign
    proforma ingest's duplicate flag pins (test_ingest.py's own count)."""
    counts: Counter[str] = Counter()
    texts: dict[str, str] = {}
    for row in fixture_rows():
        text = row["o_reason"]
        if text in NOT_ANSWERED | {"N/A"}:
            continue
        key = normalised(text)
        counts[key] += 1
        texts.setdefault(key, text)
    key, count = counts.most_common(1)[0]
    return texts[key], count


def test_duplicates_are_themed_once_and_their_tags_copied(db: psycopg.Connection[DictRow]) -> None:
    signed = signed_off_fixture(db)
    job = load_map_job(db, signed.job_id)
    planned = plan(db, job)
    proforma_text, copies = _proforma()
    assert copies >= 10, "the fixture repeats the proforma word for word"

    canonical = db.execute(
        """
        SELECT id FROM answer
         WHERE question_id = %s AND value_text = %s AND duplicate_of_answer_id IS NULL
        """,
        (job.question_id, proforma_text),
    ).fetchone()
    assert canonical is not None
    canonical_id = canonical["id"]
    duplicate_ids = [
        r["id"]
        for r in db.execute(
            "SELECT id FROM answer WHERE duplicate_of_answer_id = %s", (canonical_id,)
        ).fetchall()
    ]
    assert len(duplicate_ids) == copies - 1

    lease = claim(db, signed.job_id, "worker-1")
    assert lease is not None
    llm = RecordingLLM()

    assign(db, llm, lease, job, planned)

    # One model call carries the canonical id; no duplicate's id is ever sent.
    carrying = [p for p in llm.prompts if canonical_id in p.answer_ids]
    assert len(carrying) == 1
    assert not any(dup in p.answer_ids for p in llm.prompts for dup in duplicate_ids)

    canonical_tags = sorted(
        (r["theme_id"], r["job_id"], r["batch_no"])
        for r in db.execute(
            "SELECT theme_id, job_id, batch_no FROM answer_theme WHERE answer_id = %s",
            (canonical_id,),
        ).fetchall()
    )
    assert canonical_tags
    for dup_id in duplicate_ids:
        dup_tags = sorted(
            (r["theme_id"], r["job_id"], r["batch_no"])
            for r in db.execute(
                "SELECT theme_id, job_id, batch_no FROM answer_theme WHERE answer_id = %s",
                (dup_id,),
            ).fetchall()
        )
        assert dup_tags == canonical_tags

    # The total is every distinct answer plus every duplicate the whole
    # question carries, times the one key a batch the fake ever returns.
    expected = distinct_reasons()
    total_duplicates = db.execute(
        "SELECT count(*) AS n FROM answer WHERE question_id = %s AND duplicate_of_answer_id IS NOT NULL",
        (job.question_id,),
    ).fetchone()
    assert total_duplicates is not None
    total_tags = db.execute(
        "SELECT count(*) AS n FROM answer_theme WHERE theme_set_version_id = %s",
        (job.version_id,),
    ).fetchone()
    assert total_tags is not None
    assert total_tags["n"] == len(expected) + total_duplicates["n"]


def test_a_failed_batch_retries_at_size_one_and_buckets_the_answer(
    db: psycopg.Connection[DictRow],
) -> None:
    signed = signed_off_fixture(db)
    job = load_map_job(db, signed.job_id)
    planned = plan(db, job)
    # The fixture's partitions hold 9, 31, 30 and 4 distinct answers, so the
    # plan opens with a short batch; the first full batch of ten is the one
    # the fake spoils (docs/02, step 9: "retries at size 1").
    first_ten = next(i for i, batch in enumerate(planned) if len(batch.answers) == MAP_BATCH_SIZE)
    spoiled = planned[first_ten].answers
    bad = 3  # the fourth answer of that batch fails again on its own
    script: list[str | Fault] = [
        *[Fault.NONE] * first_ten,
        Fault.DROPPED_ID,
        *[Fault.DROPPED_ID if i == bad else Fault.NONE for i in range(MAP_BATCH_SIZE)],
        *[Fault.NONE] * (len(planned) - first_ten - 1),
    ]
    llm = FakeLLM(script)
    lease = claim(db, signed.job_id, "worker-1")
    assert lease is not None
    start_map_themes(db, job.question_id)

    ran = assign(db, llm, lease, job, planned)
    finish_map_themes(db, lease, job.question_id, job.consultation_id)

    # Every planned batch once, then the spoiled batch's ten answers one at a
    # time in the batch's own order, against the same enum.
    assert not llm.script
    assert len(llm.prompts) == len(planned) + MAP_BATCH_SIZE
    retries = llm.prompts[first_ten + 1 : first_ten + 1 + MAP_BATCH_SIZE]
    assert [p.answer_ids for p in retries] == [(a.id,) for a in spoiled]
    assert {p.theme_keys for p in retries} == {llm.prompts[first_ten].theme_keys}

    # The refused batch of ten writes no checkpoint. Each retry is its own:
    # nine done, and one unprocessable naming the answer that failed twice.
    checkpoints = db.execute(
        "SELECT batch_no, answer_ids, status FROM job_batch WHERE job_id = %s ORDER BY batch_no",
        (signed.job_id,),
    ).fetchall()
    expected = (
        [(tuple(a.id for a in b.answers), "done") for b in planned[:first_ten]]
        + [((a.id,), "unprocessable" if i == bad else "done") for i, a in enumerate(spoiled)]
        + [(tuple(a.id for a in b.answers), "done") for b in planned[first_ten + 1 :]]
    )
    assert [(tuple(c["answer_ids"]), c["status"]) for c in checkpoints] == expected
    assert [c["batch_no"] for c in checkpoints] == list(range(1, len(expected) + 1))
    assert ran == len(expected)

    # Nine neighbours tagged, the tenth not, and none of its duplicates
    # either: every distinct answer but one, plus every other duplicate.
    failed = spoiled[bad].id
    tagged = {
        r["answer_id"]
        for r in db.execute(
            "SELECT answer_id FROM answer_theme WHERE theme_set_version_id = %s",
            (job.version_id,),
        ).fetchall()
    }
    assert {a.id for a in spoiled} - tagged == {failed}
    others = db.execute(
        """
        SELECT count(*) AS n FROM answer
         WHERE question_id = %s AND duplicate_of_answer_id IS NOT NULL
           AND duplicate_of_answer_id <> %s
        """,
        (job.question_id, failed),
    ).fetchone()
    assert others is not None
    total = db.execute(
        "SELECT count(*) AS n FROM answer_theme WHERE theme_set_version_id = %s",
        (job.version_id,),
    ).fetchone()
    assert total is not None
    assert total["n"] == len(distinct_reasons()) - 1 + others["n"]

    # The job still succeeds, and the check's code is on no row (CLAUDE.md,
    # rule 8: job.error_code is for the job's own failure).
    row = db.execute(
        "SELECT status, error_code FROM job WHERE id = %s", (signed.job_id,)
    ).fetchone()
    assert row == {"status": "succeeded", "error_code": None}


def _covered(db: psycopg.Connection[DictRow], job_id: UUID) -> Counter[int]:
    """Every answer id a checkpoint of the job names, done or unprocessable,
    with how many checkpoints name it."""
    return Counter(
        int(r["answer_id"])
        for r in db.execute(
            "SELECT unnest(answer_ids) AS answer_id FROM job_batch WHERE job_id = %s", (job_id,)
        ).fetchall()
    )


def test_mapping_resumes_by_coverage_after_a_takeover(db: psycopg.Connection[DictRow]) -> None:
    signed = signed_off_fixture(db)
    job = load_map_job(db, signed.job_id)
    planned = plan(db, job)
    first_ten = next(i for i, batch in enumerate(planned) if len(batch.answers) == MAP_BATCH_SIZE)

    # The first worker: the batch of ten refused, its answers sent one at a
    # time with the second refused again, and a crash after the fourth of
    # those commits. The checkpoints now cover part of a planned batch, one
    # answer at a time, under batch numbers the plan doesn't have.
    first = claim(db, signed.job_id, "worker-1")
    assert first is not None
    script: list[str | Fault] = [
        *[Fault.NONE] * first_ten,
        Fault.DROPPED_ID,
        Fault.NONE,
        Fault.DROPPED_ID,
        Fault.NONE,
        Fault.NONE,
    ]
    llm = FakeLLM(script)
    with pytest.raises(WorkerCrashError):
        assign(db, llm, first, job, planned, after_batch=crash_after(first_ten + 4))
    assert not llm.script
    before = _covered(db, signed.job_id)
    assert sum(before.values()) == sum(len(b.answers) for b in planned[:first_ten]) + 4

    # Ten minutes of silence, and a second worker takes over (ADR-002).
    db.execute(
        "UPDATE job SET heartbeat_at = now() - interval '11 minutes' WHERE id = %s",
        (signed.job_id,),
    )
    second = claim(db, signed.job_id, "worker-2")
    assert second is not None and second.fence == 2
    llm2 = RecordingLLM()

    assign(db, llm2, second, job, plan(db, job))

    # The second worker sends only what no checkpoint covers, the answer in
    # the unprocessable bucket counting as covered, and ten at most a call.
    sent = [answer_id for p in llm2.prompts for answer_id in p.answer_ids]
    assert sent
    assert not set(sent) & before.keys()
    assert all(len(p.answer_ids) <= MAP_BATCH_SIZE for p in llm2.prompts)

    # Between them the checkpoints name each planned answer exactly once.
    assert _covered(db, signed.job_id) == Counter(a.id for b in planned for a in b.answers)

    # One tag per distinct answer for the one key the fake returns, bar the
    # answer in the bucket: counted, not left to the unique index.
    tags = db.execute(
        """
        SELECT count(*) AS n, count(DISTINCT (t.answer_id, t.theme_id)) AS pairs
          FROM answer_theme t JOIN answer a ON a.id = t.answer_id
         WHERE t.theme_set_version_id = %s AND a.duplicate_of_answer_id IS NULL
        """,
        (job.version_id,),
    ).fetchone()
    assert tags == {"n": len(distinct_reasons()) - 1, "pairs": len(distinct_reasons()) - 1}
