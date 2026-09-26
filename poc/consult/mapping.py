"""The map_themes job, stage by stage (docs/02, step 9; ADR-002).

Same shape as `themes.py`: the stages take a connection and never commit,
so the caller owns the transaction and commits between batches. The plan
reuses `themes.batches` at `MAP_BATCH_SIZE`, the same partition (by
related closed answer) and shuffle (by the stored seed) a find_themes job
uses, so a signed-off question's answers are covered exactly once, no
duplicate's copy. A batch's tags and its checkpoint go in one transaction;
`finish_map_themes` (the next chunk) starts a fresh one that locks the
consultation first, so nothing here may take that lock.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from consult import jobs, themes
from consult.jobs import Lease
from consult.llm import LLM
from consult.prompts import map_themes_prompt
from consult.replies import parse_assignments
from consult.tags import Tag, insert_tags

# docs/02, step 9: batches of ten, a blast-radius decision. An injected
# instruction the model obeys can spoil at most ten answers this way, and
# the retry at size one (the next chunk) isolates the one that carried it.
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
    transaction per batch the tags through tags.insert_tags (fenced;
    source 'ai', this job and batch) and a checkpoint. `after_batch` is
    where the caller commits. Returns the number of batches this call ran.

    The batch number comes from `jobs.next_batch_no` when it is written,
    not from the plan's position, because a later chunk's retry at size
    one adds batches out of the plan's order (plan section 2, "resume is
    by coverage")."""
    shortlist = _shortlist(conn, job.version_id)
    theme_list = [(str(r["key"]), str(r["label"]), r["description"]) for r in shortlist]
    theme_ids = {str(r["key"]): r["id"] for r in shortlist}
    ran = 0
    for batch in plan:
        prompt = map_themes_prompt(
            model_alias=job.model_alias,
            question_text=job.question_text,
            related_answer=batch.related_answer,
            themes=theme_list,
            answers=batch.answers,
        )
        completion = llm.complete(prompt)
        assignments = parse_assignments(completion, prompt)
        canonical = [
            Tag(assignment.answer_id, theme_ids[key])
            for assignment in assignments
            for key in assignment.theme_keys
        ]
        batch_no = jobs.next_batch_no(conn, lease.job_id)
        insert_tags(conn, lease, job.version_id, batch_no=batch_no, tags=canonical)
        jobs.checkpoint(
            conn,
            lease,
            batch_no=batch_no,
            stage="map",
            answer_ids=[answer.id for answer in batch.answers],
            trace_id=completion.trace_id,
            tokens_in=completion.tokens_in,
            tokens_out=completion.tokens_out,
        )
        ran += 1
        if after_batch is not None:
            after_batch(batch_no)
    return ran
