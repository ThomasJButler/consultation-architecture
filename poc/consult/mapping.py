"""The map_themes job, stage by stage (docs/02, step 9; ADR-002).

Same shape as `themes.py`: the stages take a connection and never commit,
so the caller owns the transaction and commits between batches. The plan
reuses `themes.batches` at `MAP_BATCH_SIZE`, the same partition (by
related closed answer) and shuffle (by the stored seed) a find_themes job
uses, so a signed-off question's distinct answers are planned and sent to
the model exactly once; their exact duplicates are never sent, and get
their canonical's tags copied instead. A batch's tags and its checkpoint
go in one transaction; `finish_map_themes` (the next chunk) starts a
fresh one that locks the consultation first, so nothing here may take
that lock.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from consult import jobs, logs, themes
from consult.jobs import Lease
from consult.llm import LLM, Completion, Prompt
from consult.prompts import PromptAnswer, map_themes_prompt
from consult.replies import Assignment, Reason, ReplyError, parse_assignments
from consult.tags import Tag, insert_tags

logger = logging.getLogger(__name__)

# docs/02, step 9: batches of ten, a blast-radius decision. An injected
# instruction the model obeys can spoil at most ten answers this way, and
# the retry at size one in `assign` isolates the one that carried it.
MAP_BATCH_SIZE = 10


@dataclass(frozen=True)
class MapThemesJob:
    """The map_themes job row, the question it maps and the signed-off
    version its shortlist comes from."""

    job_id: UUID
    question_id: UUID
    consultation_id: UUID
    question_text: str
    version_id: UUID
    model_alias: str
    seed: int


def load_map_job(conn: psycopg.Connection[DictRow], job_id: UUID) -> MapThemesJob:
    """The job as the runner needs it: like themes.load_job, but the
    version is found rather than made, since sign-off froze it (docs/02,
    step 8). LookupError when the alias, the seed or the version is
    missing, which is what a job dispatch hasn't reached yet looks like."""
    row = conn.execute(
        """
        SELECT j.id, j.question_id, j.consultation_id, j.model_alias, j.params, q.question_text,
               (SELECT id FROM theme_set_version
                 WHERE question_id = j.question_id AND status = 'signed_off'
                 ORDER BY version_no DESC LIMIT 1) AS version_id
          FROM job j JOIN question q ON q.id = j.question_id
         WHERE j.id = %s AND j.kind = 'map_themes'
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"job {job_id} is not a map_themes job")
    seed = row["params"].get("seed")
    if row["model_alias"] is None or not isinstance(seed, int) or row["version_id"] is None:
        raise LookupError(f"job {job_id} has no model alias, seed or signed-off version yet")
    return MapThemesJob(
        job_id=row["id"],
        question_id=row["question_id"],
        consultation_id=row["consultation_id"],
        question_text=row["question_text"],
        version_id=row["version_id"],
        model_alias=row["model_alias"],
        seed=seed,
    )


def plan(conn: psycopg.Connection[DictRow], job: MapThemesJob) -> list[themes.Batch]:
    """Step 9's batches: distinct answers only, partitioned by the related
    closed answer and shuffled with the stored seed, cut at
    `MAP_BATCH_SIZE` (docs/02, step 9; exact duplicates were flagged at
    ingest and are themed once, docs/02 step 6)."""
    return themes.batches(conn, job.question_id, seed=job.seed, size=MAP_BATCH_SIZE)


def _duplicate_tags(
    conn: psycopg.Connection[DictRow],
    answer_ids: Sequence[int],
    canonical_keys: dict[int, tuple[str, ...]],
    theme_ids: dict[str, UUID],
) -> list[Tag]:
    """Every exact duplicate of the batch's answers, carrying its
    canonical's theme keys (docs/02, step 9: duplicates are themed once
    and the tags copied; answer.duplicate_of_answer_id, ingest's flag)."""
    rows = conn.execute(
        "SELECT id, duplicate_of_answer_id FROM answer WHERE duplicate_of_answer_id = ANY(%s::bigint[])",
        (list(answer_ids),),
    ).fetchall()
    return [
        Tag(int(row["id"]), theme_ids[key])
        for row in rows
        for key in canonical_keys.get(int(row["duplicate_of_answer_id"]), ())
    ]


def _shortlist(conn: psycopg.Connection[DictRow], version_id: UUID) -> list[DictRow]:
    # v2 carries the longlist too (sign_off copies every row for lineage);
    # only the shortlist plus its two fallbacks are the enum to map against.
    return conn.execute(
        """
        SELECT id, key, label, description FROM theme
         WHERE theme_set_version_id = %s AND NOT is_longlist ORDER BY key
        """,
        (version_id,),
    ).fetchall()


def _checked(completion: Completion, prompt: Prompt) -> tuple[Assignment, ...] | Reason:
    """The reply's assignments, or the reason the check refused it. Only
    the reason leaves, so no write after a refusal can chain to the reply
    (a JSONDecodeError carries the whole document on `.doc`)."""
    try:
        return parse_assignments(completion, prompt)
    except ReplyError as exc:
        return exc.reason


def _send(
    conn: psycopg.Connection[DictRow],
    llm: LLM,
    lease: Lease,
    job: MapThemesJob,
    shortlist: Sequence[DictRow],
    related: str | None,
    answers: Sequence[PromptAnswer],
    after_batch: Callable[[int], None] | None,
) -> bool:
    """One call for `answers` and, in one transaction, what it writes: the
    tags (fenced; source 'ai', this job and batch), the duplicates' copies
    and a done checkpoint, or for a lone answer the check refuses, an
    unprocessable checkpoint naming it and no tag (docs/02, step 9). The
    refusal's code goes to the log and nowhere else. False, with nothing
    written, when the check refuses more than one answer: the caller's cue
    to retry them one at a time."""
    prompt = map_themes_prompt(
        model_alias=job.model_alias,
        question_text=job.question_text,
        related_answer=related,
        themes=[(str(r["key"]), str(r["label"]), r["description"]) for r in shortlist],
        answers=answers,
    )
    completion = llm.complete(prompt)
    checked = _checked(completion, prompt)
    answer_ids = [answer.id for answer in answers]
    if isinstance(checked, Reason):
        # A refused batch of more than one gets no checkpoint of its own, so
        # this line is the one place its trace id is kept.
        logs.log_event(
            logger,
            "map_reply_refused",
            level=logging.WARNING,
            job_id=lease.job_id,
            answer_count=len(answer_ids),
            error_code=ReplyError.code,
            reason_code=checked,
            trace_id=completion.trace_id,
        )
        if len(answer_ids) > 1:
            return False
        batch_no = jobs.next_batch_no(conn, lease.job_id)
        jobs.checkpoint(
            conn,
            lease,
            batch_no=batch_no,
            stage="map",
            answer_ids=answer_ids,
            status="unprocessable",
            trace_id=completion.trace_id,
            tokens_in=completion.tokens_in,
            tokens_out=completion.tokens_out,
        )
    else:
        theme_ids = {str(r["key"]): r["id"] for r in shortlist}
        canonical_keys = {assignment.answer_id: assignment.theme_keys for assignment in checked}
        canonical = [
            Tag(assignment.answer_id, theme_ids[key])
            for assignment in checked
            for key in assignment.theme_keys
        ]
        duplicates = _duplicate_tags(conn, answer_ids, canonical_keys, theme_ids)
        batch_no = jobs.next_batch_no(conn, lease.job_id)
        insert_tags(conn, lease, job.version_id, batch_no=batch_no, tags=canonical + duplicates)
        jobs.checkpoint(
            conn,
            lease,
            batch_no=batch_no,
            stage="map",
            answer_ids=answer_ids,
            trace_id=completion.trace_id,
            tokens_in=completion.tokens_in,
            tokens_out=completion.tokens_out,
        )
    if after_batch is not None:
        after_batch(batch_no)
    return True


def assign(
    conn: psycopg.Connection[DictRow],
    llm: LLM,
    lease: Lease,
    job: MapThemesJob,
    plan: Sequence[themes.Batch],
    *,
    after_batch: Callable[[int], None] | None = None,
) -> int:
    """Step 9's mapping: one map_themes_prompt per batch against v2's
    (key, label, description) list, parse_assignments, then in one
    transaction per batch the tags and a checkpoint. `after_batch` is
    where the caller commits. A batch the check refuses is sent again one
    answer at a time, each its own checkpoint, so a spoiled batch costs
    the answer that spoiled it and not its nine neighbours (docs/02, step
    9). Returns the number of checkpoints this call wrote.

    The batch number comes from `jobs.next_batch_no` when it is written,
    not from the plan's position, because the retry at size one adds
    batches the plan doesn't have (plan section 2, "resume is by
    coverage")."""
    shortlist = _shortlist(conn, job.version_id)
    ran = 0
    for batch in plan:
        if _send(
            conn, llm, lease, job, shortlist, batch.related_answer, batch.answers, after_batch
        ):
            ran += 1
            continue
        for answer in batch.answers:
            _send(conn, llm, lease, job, shortlist, batch.related_answer, (answer,), after_batch)
            ran += 1
    return ran
