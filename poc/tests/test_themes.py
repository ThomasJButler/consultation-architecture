"""What the find_themes job promises (docs/02, steps 6 and 7; ADR-002).

Driven stage by stage with the model a fake: batches of distinct answers,
generation, condensation to a capped shortlist with lineage, a preview
that gives every candidate a count and quotes, then v1 and fan-in 1 in one
transaction, with a checkpoint per batch so a takeover resumes rather than
repeats. Every expectation is read from the fixture CSV or the fakes'
script, not from the code under test.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from uuid import UUID

import psycopg
import pytest
from psycopg.errors import UniqueViolation
from psycopg.rows import DictRow

from consult.ingest import ingest
from consult.jobs import claim
from consult.prompts import DATA_PREAMBLE, PromptAnswer
from consult.themes import (
    BATCH_SIZE,
    EXAMPLES_PER_THEME,
    PREVIEW_BATCH_SIZE,
    SAMPLE_SIZE,
    SHORTLIST_CAP,
    batches,
    condense,
    ensure_version,
    generate,
    load_job,
    preview,
    run_find_themes,
    stratified_sample,
)
from consult.transitions import Advance, finish_find_themes, start_find_themes
from tests.fakes import RecordingLLM
from tests.pipeline import NOT_ANSWERED, Staged, fixture_rows, staged_fixture

pytestmark = pytest.mark.db

ROUTE_OPTIONS = {"Support", "Oppose", "Not sure"}


def normalised(text: str) -> str:
    return " ".join(text.split()).casefold()


def distinct_reasons() -> list[tuple[int, str, str | None]]:
    """(row number, text, related c_route answer or None) for every first
    occurrence of an o_reason text, as ingest is pinned to keep them."""
    seen: set[str] = set()
    rows = []
    for no, row in enumerate(fixture_rows(), start=2):
        text = row["o_reason"]
        if text in NOT_ANSWERED | {"N/A"} or normalised(text) in seen:
            continue
        seen.add(normalised(text))
        related = row["c_route"] if row["c_route"] in ROUTE_OPTIONS else None
        rows.append((no, text, related))
    return rows


def test_generation_batches_distinct_answers_by_count_and_cap(
    db: psycopg.Connection[DictRow],
) -> None:
    staged = staged_fixture(db)
    ingest(db, staged.consultation_id)
    question_id = staged.configured.questions["o_reason"]
    expected = distinct_reasons()
    assert 50 < len(expected) < 240, "the proforma and the repeats should thin the 240"

    planned = batches(db, question_id, seed=7)

    # Every distinct answer once, no duplicate's copy, no blank (docs/02, step
    # 6: exact duplicates were flagged at ingest and are themed once).
    texts = sorted(answer.text for batch in planned for answer in batch.answers)
    assert texts == sorted(text for _no, text, _related in expected)
    ids = [answer.id for batch in planned for answer in batch.answers]
    assert len(ids) == len(set(ids))
    # Partitioned by the related closed answer, so one fill of the
    # placeholder serves a whole batch; a blank or unresolved c_route is its
    # own partition with nothing to fill.
    for batch in planned:
        in_batch = {a.text for a in batch.answers}
        related_of_batch = {r for _no, text, r in expected if text in in_batch}
        assert related_of_batch == {batch.related_answer}
    per_partition = Counter(batch.related_answer for batch in planned)
    assert set(per_partition) == {"Support", "Oppose", "Not sure", None}
    sizes = Counter(related for _no, _text, related in expected)
    for partition, count in sizes.items():
        in_batches = sum(len(b.answers) for b in planned if b.related_answer == partition)
        assert in_batches == count
    # At most fifty per batch (docs/02, step 6), and the plan fills a batch
    # before it starts another.
    assert BATCH_SIZE == 50
    assert all(len(batch.answers) <= 50 for batch in planned)
    assert len(planned) == sum(-(-count // 50) for count in sizes.values())

    # The same seed gives the same order twice; a different seed doesn't.
    assert batches(db, question_id, seed=7) == planned
    assert batches(db, question_id, seed=8) != planned

    # A token cap splits a batch before the count does: at four characters
    # a token and a cap of 300 tokens, no batch carries more than that.
    capped = batches(db, question_id, seed=7, token_cap=300)
    assert len(capped) > len(planned)
    for batch in capped:
        assert (
            sum(max(1, len(a.text) // 4) for a in batch.answers) <= 300 or len(batch.answers) == 1
        )
    assert sorted(a.id for b in capped for a in b.answers) == sorted(ids)


class WorkerCrashError(Exception):
    """A worker dying between batches."""


def crash_after(batches_done: int) -> Callable[[int], None]:
    def after_batch(batch_no: int) -> None:
        if batch_no >= batches_done:
            raise WorkerCrashError(batch_no)

    return after_batch


def queued_find_themes_job(
    db: psycopg.Connection[DictRow], staged: Staged, column_ref: str
) -> UUID:
    """The job ingest inserted for the question, moved to queued by hand
    (dispatch is PR-08's) with the alias and seed the runner will set."""
    question_id = staged.configured.questions[column_ref]
    row = db.execute(
        """
        UPDATE job SET status = 'queued', model_alias = 'fake-model', params = '{"seed": 7}'
         WHERE question_id = %s AND kind = 'find_themes'
        RETURNING id
        """,
        (question_id,),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def test_find_themes_checkpoints_every_batch_and_resumes(db: psycopg.Connection[DictRow]) -> None:
    staged = staged_fixture(db)
    ingest(db, staged.consultation_id)
    job_id = queued_find_themes_job(db, staged, "o_reason")
    job = load_job(db, job_id)
    assert (job.model_alias, job.seed) == ("fake-model", 7)
    plan = batches(db, job.question_id, seed=job.seed)
    assert len(plan) == 4

    # The first worker claims, runs two of the four batches and dies.
    first = claim(db, job_id, "worker-1")
    assert first is not None
    version_id = ensure_version(db, first, job.question_id)
    llm = RecordingLLM()
    with pytest.raises(WorkerCrashError):
        generate(db, llm, first, job, version_id, plan, after_batch=crash_after(2))
    checkpoints = db.execute(
        "SELECT batch_no, stage, answer_ids, status FROM job_batch WHERE job_id = %s ORDER BY batch_no",
        (job_id,),
    ).fetchall()
    assert [(c["batch_no"], c["stage"], c["status"]) for c in checkpoints] == [
        (1, "generate", "done"),
        (2, "generate", "done"),
    ]
    assert [tuple(c["answer_ids"]) for c in checkpoints] == [
        tuple(a.id for a in batch.answers) for batch in plan[:2]
    ]
    # Each prompt was the contract for its batch: the placeholder filled
    # from the batch's related answer, or marked not answered.
    for prompt, batch in zip(llm.prompts, plan[:2], strict=True):
        assert (batch.related_answer or "(not answered)") in prompt.system
        assert prompt.answer_ids == tuple(a.id for a in batch.answers)

    # Ten minutes of silence, and a second worker takes over (ADR-002). It
    # rebuilds the plan from the seed and the fake sees only the two
    # batches the first worker never finished.
    db.execute(
        "UPDATE job SET heartbeat_at = now() - interval '11 minutes' WHERE id = %s", (job_id,)
    )
    second = claim(db, job_id, "worker-2")
    assert second is not None and second.fence == 2
    assert ensure_version(db, second, job.question_id) == version_id
    llm2 = RecordingLLM()
    generate(db, llm2, second, job, version_id, batches(db, job.question_id, seed=job.seed))
    assert [p.answer_ids for p in llm2.prompts] == [
        tuple(a.id for a in b.answers) for b in plan[2:]
    ]
    checkpoints = db.execute(
        "SELECT batch_no, stage FROM job_batch WHERE job_id = %s ORDER BY batch_no", (job_id,)
    ).fetchall()
    assert [(c["batch_no"], c["stage"]) for c in checkpoints] == [
        (n, "generate") for n in (1, 2, 3, 4)
    ]

    # Every batch's three candidates are in v1 as the longlist, each key
    # once, and nothing is marked shortlist yet.
    candidates = db.execute(
        "SELECT key, is_longlist, lineage_theme_id FROM theme WHERE theme_set_version_id = %s ORDER BY key",
        (version_id,),
    ).fetchall()
    assert len(candidates) == 12
    assert {c["is_longlist"] for c in candidates} == {True}
    assert {c["lineage_theme_id"] for c in candidates} == {None}
    expected_keys = {
        f"{stem}_{min(batch.answers, key=lambda a: a.id).id}"
        for batch in plan
        for stem in ("SAFETY", "PARKING", "ACCESS")
    }
    assert {c["key"] for c in candidates} == expected_keys
    version = db.execute(
        "SELECT version_no, status FROM theme_set_version WHERE id = %s", (version_id,)
    ).fetchone()
    assert version == {"version_no": 1, "status": "candidate"}


def test_condense_caps_the_shortlist_and_keeps_lineage(db: psycopg.Connection[DictRow]) -> None:
    staged = staged_fixture(db)
    ingest(db, staged.consultation_id)
    job_id = queued_find_themes_job(db, staged, "o_reason")
    job = load_job(db, job_id)
    lease = claim(db, job_id, "worker-1")
    assert lease is not None
    version_id = ensure_version(db, lease, job.question_id)
    llm = RecordingLLM()
    generated = generate(
        db, llm, lease, job, version_id, batches(db, job.question_id, seed=job.seed)
    )
    assert generated == 4

    shortlist = condense(db, llm, lease, job, version_id, generated=generated)

    # Twelve candidates in three stems, so three shortlist themes (docs/02,
    # step 6: about thirty, cap seventy; the fake folds by key stem).
    assert shortlist == 3 and SHORTLIST_CAP == 70
    prompt = llm.prompts[-1]
    assert DATA_PREAMBLE in prompt.system and prompt.answer_ids == () and prompt.theme_keys == ()
    themes = db.execute(
        "SELECT id, key, is_longlist, lineage_theme_id FROM theme WHERE theme_set_version_id = %s ORDER BY key",
        (version_id,),
    ).fetchall()
    sent = json.loads(prompt.user)
    assert sorted(c["key"] for c in sent) == sorted(t["key"] for t in themes if t["is_longlist"])
    short = {t["key"]: t for t in themes if not t["is_longlist"]}
    assert set(short) == {"ACCESS", "PARKING", "SAFETY"}
    longlist = [t for t in themes if t["is_longlist"]]
    assert len(longlist) == 12
    # The longlist is kept, each candidate pointing at the theme it folded into.
    for candidate in longlist:
        stem = candidate["key"].rsplit("_", 1)[0]
        assert candidate["lineage_theme_id"] == short[stem]["id"]
    assert {t["lineage_theme_id"] for t in short.values()} == {None}
    checkpoints = db.execute(
        "SELECT batch_no, stage, answer_ids FROM job_batch WHERE job_id = %s ORDER BY batch_no",
        (job_id,),
    ).fetchall()
    assert [(c["batch_no"], c["stage"]) for c in checkpoints][-1] == (5, "condense")
    assert checkpoints[-1]["answer_ids"] == []
    # Called again after its checkpoint: no model call, nothing changed.
    calls = len(llm.prompts)
    assert condense(db, llm, lease, job, version_id, generated=generated) == 3
    assert len(llm.prompts) == calls
    assert db.execute(
        "SELECT count(*) AS n FROM job_batch WHERE job_id = %s", (job_id,)
    ).fetchone() == {"n": 5}

    # The cap, on the other question: most merges first, ties by key, and the
    # candidates of a theme past the cap stay longlist with no lineage.
    other_id = queued_find_themes_job(db, staged, "o_safety")
    other = load_job(db, other_id)
    other_lease = claim(db, other_id, "worker-1")
    assert other_lease is not None
    other_version = ensure_version(db, other_lease, other.question_id)
    other_generated = generate(
        db, llm, other_lease, other, other_version, batches(db, other.question_id, seed=other.seed)
    )
    assert (
        condense(db, llm, other_lease, other, other_version, generated=other_generated, cap=2) == 2
    )
    rows = db.execute(
        "SELECT key, is_longlist, lineage_theme_id FROM theme WHERE theme_set_version_id = %s ORDER BY key",
        (other_version,),
    ).fetchall()
    assert [r["key"] for r in rows if not r["is_longlist"]] == ["ACCESS", "PARKING"]
    dropped = [r for r in rows if r["is_longlist"] and r["key"].startswith("SAFETY_")]
    assert dropped and {r["lineage_theme_id"] for r in dropped} == {None}
    kept = [r for r in rows if r["is_longlist"] and not r["key"].startswith("SAFETY_")]
    assert kept and None not in {r["lineage_theme_id"] for r in kept}


def test_the_sample_is_stratified_by_the_related_answer() -> None:
    # Proportional to each stratum, every non-empty stratum represented,
    # the same draw from the same seed, and everyone when there are fewer
    # answers than the sample asks for (docs/02, step 6: 200 answers).
    strata: dict[str | None, list[PromptAnswer]] = {
        "Support": [PromptAnswer(n, f"s{n}") for n in range(100)],
        "Oppose": [PromptAnswer(n, f"o{n}") for n in range(100, 150)],
        None: [PromptAnswer(n, f"n{n}") for n in range(150, 200)],
    }
    drawn = stratified_sample(strata, size=20, seed=7)
    assert {k: len(v) for k, v in drawn.items()} == {"Support": 10, "Oppose": 5, None: 5}
    assert all(a in strata[k] for k, v in drawn.items() for a in v)
    assert stratified_sample(strata, size=20, seed=7) == drawn
    assert stratified_sample(strata, size=20, seed=8) != drawn
    small: dict[str | None, list[PromptAnswer]] = {
        "Support": strata["Support"][:3],
        None: strata[None][:2],
    }
    assert stratified_sample(small, size=200, seed=7) == small
    tiny: dict[str | None, list[PromptAnswer]] = {
        "A": strata["Support"][:50],
        "B": strata["Oppose"][:1],
    }
    assert {k: len(v) for k, v in stratified_sample(tiny, size=5, seed=1).items()} == {
        "A": 4,
        "B": 1,
    }


def test_preview_gives_every_candidate_a_count_and_quotes(db: psycopg.Connection[DictRow]) -> None:
    staged = staged_fixture(db)
    ingest(db, staged.consultation_id)
    job_id = queued_find_themes_job(db, staged, "o_reason")
    job = load_job(db, job_id)
    lease = claim(db, job_id, "worker-1")
    assert lease is not None
    version_id = ensure_version(db, lease, job.question_id)
    llm = RecordingLLM()
    plan = batches(db, job.question_id, seed=job.seed)
    generated = generate(db, llm, lease, job, version_id, plan)
    assert condense(db, llm, lease, job, version_id, generated=generated) == 3
    distinct = len(distinct_reasons())
    assert distinct < SAMPLE_SIZE == 200, (
        "fewer distinct answers than the sample, so everyone is in"
    )
    calls_before = len(llm.prompts)

    # The first worker previews two batches and dies; the second finishes.
    with pytest.raises(WorkerCrashError):
        preview(db, llm, lease, job, version_id, generated=generated, after_batch=crash_after(2))
    db.execute(
        "UPDATE job SET heartbeat_at = now() - interval '11 minutes' WHERE id = %s", (job_id,)
    )
    second = claim(db, job_id, "worker-2")
    assert second is not None
    llm2 = RecordingLLM()
    previewed = preview(db, llm2, second, job, version_id, generated=generated)

    prompts = llm.prompts[calls_before:] + llm2.prompts
    per_partition = Counter(related for _no, _text, related in distinct_reasons())
    assert PREVIEW_BATCH_SIZE == 10
    assert len(prompts) == sum(-(-count // 10) for count in per_partition.values())
    # Every preview call maps at most ten answers against the shortlist enum
    # (docs/02, step 9's shape; docs/06, section 2.2), placeholder filled.
    assert all(len(p.answer_ids) <= 10 for p in prompts)
    assert {p.theme_keys for p in prompts} == {("ACCESS", "PARKING", "SAFETY")}
    seen = sorted(answer_id for p in prompts for answer_id in p.answer_ids)
    assert seen == sorted(a.id for b in plan for a in b.answers)
    assert previewed + 20 == distinct or previewed == distinct - sum(
        len(p.answer_ids) for p in llm.prompts[calls_before:]
    )
    # The fake labels every answer with the first key, so ACCESS carries
    # the whole sample and the other two carry zero rather than nothing.
    counts = db.execute(
        "SELECT key, preview_count FROM theme WHERE theme_set_version_id = %s AND NOT is_longlist ORDER BY key",
        (version_id,),
    ).fetchall()
    assert [(c["key"], c["preview_count"]) for c in counts] == [
        ("ACCESS", distinct),
        ("PARKING", 0),
        ("SAFETY", 0),
    ]
    examples = db.execute(
        """
        SELECT t.key, e.rank, e.answer_id FROM theme_example e JOIN theme t ON t.id = e.theme_id
         WHERE t.theme_set_version_id = %s ORDER BY t.key, e.rank
        """,
        (version_id,),
    ).fetchall()
    assert EXAMPLES_PER_THEME == 3
    assert [(e["key"], e["rank"]) for e in examples] == [
        ("ACCESS", 1),
        ("ACCESS", 2),
        ("ACCESS", 3),
    ]
    assert {e["answer_id"] for e in examples} <= set(seen)
    # One checkpoint per preview batch, numbered on from the condense one.
    checkpoints = db.execute(
        "SELECT batch_no, stage, answer_ids FROM job_batch WHERE job_id = %s ORDER BY batch_no",
        (job_id,),
    ).fetchall()
    stages = [c["stage"] for c in checkpoints]
    assert stages[: generated + 1] == ["generate"] * generated + ["condense"]
    assert stages[generated + 1 :] == ["preview"] * len(prompts)
    assert (
        sorted(a for c in checkpoints if c["stage"] == "preview" for a in c["answer_ids"]) == seen
    )
    # And nothing counted twice across the takeover: the count is the sample.
    assert sum(c["preview_count"] for c in counts) == distinct


def test_find_themes_writes_v1_and_flips_the_question_in_one_transaction(
    db: psycopg.Connection[DictRow],
) -> None:
    staged = staged_fixture(db)
    ingest(db, staged.consultation_id)
    consultation_id = staged.consultation_id

    # The first question by its stages. Claiming moves the question on
    # (docs/02, section 6); a takeover finds it already moved and carries on.
    job_id = queued_find_themes_job(db, staged, "o_reason")
    job = load_job(db, job_id)
    lease = claim(db, job_id, "worker-1")
    assert lease is not None
    assert start_find_themes(db, job.question_id) is True
    assert start_find_themes(db, job.question_id) is False
    version_id = ensure_version(db, lease, job.question_id)
    llm = RecordingLLM()
    plan = batches(db, job.question_id, seed=job.seed)
    generated = generate(db, llm, lease, job, version_id, plan)
    condense(db, llm, lease, job, version_id, generated=generated)
    preview(db, llm, lease, job, version_id, generated=generated)

    advance = finish_find_themes(db, lease, job.question_id, consultation_id)

    # The question is themes_ready and the job succeeded together; the
    # consultation waits for its sibling, so no email yet (docs/02, step 7).
    assert advance == Advance(themes_ready=False, analysis_ready=False)
    state = db.execute(
        """
        SELECT q.status AS question, j.status AS job, c.status AS consultation,
               (SELECT count(*) FROM notification_outbox WHERE consultation_id = c.id) AS emails
          FROM question q JOIN job j ON j.id = %s JOIN consultation c ON c.id = q.consultation_id
         WHERE q.id = %s
        """,
        (job_id, job.question_id),
    ).fetchone()
    assert state == {
        "question": "themes_ready",
        "job": "succeeded",
        "consultation": "processing",
        "emails": 0,
    }
    versions = db.execute(
        "SELECT version_no, status FROM theme_set_version WHERE question_id = %s",
        (job.question_id,),
    ).fetchall()
    assert versions == [{"version_no": 1, "status": "candidate"}]
    shape = db.execute(
        """
        SELECT count(*) FILTER (WHERE NOT is_longlist) AS shortlist,
               count(*) FILTER (WHERE is_longlist) AS longlist,
               count(*) FILTER (WHERE NOT is_longlist AND preview_count IS NULL) AS uncounted
          FROM theme WHERE theme_set_version_id = %s
        """,
        (version_id,),
    ).fetchone()
    assert shape == {"shortlist": 3, "longlist": 12, "uncounted": 0}

    # The other question through the runner, end to end: the last open
    # question flips the consultation and writes one themes_ready row
    # naming the pass (ADR-006).
    other_id = queued_find_themes_job(db, staged, "o_safety")
    other_lease = claim(db, other_id, "worker-1")
    assert other_lease is not None
    committed: list[int] = []

    advance = run_find_themes(db, RecordingLLM(), other_lease, after_batch=committed.append)

    assert advance == Advance(themes_ready=True, analysis_ready=False)
    assert committed and committed == sorted(committed)
    after = db.execute(
        """
        SELECT c.status, o.kind, o.subject_id = c.run_id AS this_pass,
               (SELECT status FROM question WHERE id = %(q)s) AS question,
               (SELECT status FROM job WHERE id = %(j)s) AS job,
               (SELECT count(*) FROM theme_set_version WHERE question_id = %(q)s) AS versions
          FROM consultation c JOIN notification_outbox o ON o.consultation_id = c.id
         WHERE c.id = %(c)s
        """,
        {"q": load_job(db, other_id).question_id, "j": other_id, "c": consultation_id},
    ).fetchall()
    assert after == [
        {
            "status": "awaiting_review",
            "kind": "themes_ready",
            "this_pass": True,
            "question": "themes_ready",
            "job": "succeeded",
            "versions": 1,
        }
    ]


def test_a_second_delivery_writes_no_second_v1(db: psycopg.Connection[DictRow]) -> None:
    # The first worker checkpoints every batch and dies before its last
    # transaction; the second runs the whole job again and finds it all
    # already there (docs/04, section 3: a find_themes job delivered twice
    # can't write a second v1).
    staged = staged_fixture(db)
    ingest(db, staged.consultation_id)
    job_id = queued_find_themes_job(db, staged, "o_reason")
    job = load_job(db, job_id)
    first = claim(db, job_id, "worker-1")
    assert first is not None
    start_find_themes(db, job.question_id)
    version_id = ensure_version(db, first, job.question_id)
    llm = RecordingLLM()
    generated = generate(
        db, llm, first, job, version_id, batches(db, job.question_id, seed=job.seed)
    )
    condense(db, llm, first, job, version_id, generated=generated)
    preview(db, llm, first, job, version_id, generated=generated)

    def written() -> tuple[list[DictRow], list[DictRow], int]:
        themes = db.execute(
            """
            SELECT id, key, label, is_longlist, lineage_theme_id, preview_count
              FROM theme WHERE theme_set_version_id = %s ORDER BY key
            """,
            (version_id,),
        ).fetchall()
        versions = db.execute(
            "SELECT id, version_no, status FROM theme_set_version WHERE question_id = %s",
            (job.question_id,),
        ).fetchall()
        row = db.execute(
            "SELECT count(*) AS n FROM job_batch WHERE job_id = %s", (job_id,)
        ).fetchone()
        assert row is not None
        return themes, versions, int(row["n"])

    before = written()
    db.execute(
        "UPDATE job SET heartbeat_at = now() - interval '11 minutes' WHERE id = %s", (job_id,)
    )
    second = claim(db, job_id, "worker-2")
    assert second is not None
    llm2 = RecordingLLM()

    advance = run_find_themes(db, llm2, second)

    assert llm2.prompts == []
    assert written() == before
    assert len(before[1]) == 1
    assert advance == Advance(themes_ready=False, analysis_ready=False)
    state = db.execute(
        "SELECT q.status AS question, j.status AS job, j.attempts FROM question q, job j WHERE q.id = %s AND j.id = %s",
        (job.question_id, job_id),
    ).fetchone()
    assert state == {"question": "themes_ready", "job": "succeeded", "attempts": 2}
    # And the index itself, should a code path ever forget the rule.
    with pytest.raises(UniqueViolation), db.transaction():
        db.execute(
            """
            INSERT INTO theme_set_version (department_id, question_id, version_no, status)
            SELECT department_id, id, 1, 'candidate' FROM question WHERE id = %s
            """,
            (job.question_id,),
        )
