"""The find_themes job, stage by stage (docs/02, step 6; ADR-002).

One `job_batch` row is one call of one stage function on one chunk. The
stages here take a connection and never commit; the caller owns the
transaction and commits between batches, which is what makes a checkpoint
worth having. The plan of batches is recomputed from a seed stored on the
job rather than stored itself, so a worker taking over can rebuild it and
start at the batch after the last checkpoint.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from consult import jobs
from consult.jobs import Lease, LeaseLostError
from consult.llm import LLM
from consult.prompts import PromptAnswer, find_themes_prompt
from consult.replies import ProposedTheme, parse_themes

# About fifty distinct answers a batch (docs/02, step 6), under a token cap
# that keeps a batch of long answers within what docs/05 budgets for a call:
# 150 tokens per open answer at four characters a token, times fifty.
BATCH_SIZE = 50
TOKEN_CAP = 7_500
CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class Batch:
    """One generation call: answers that share a related closed answer, so
    the question's placeholder is filled once for all of them (docs/00)."""

    related_answer: str | None
    answers: tuple[PromptAnswer, ...]


def _tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def distinct_answers(
    conn: psycopg.Connection[DictRow], question_id: UUID
) -> list[tuple[int, str, str | None]]:
    """(answer id, text, related closed answer) for every answer to the
    question that is not blank and not a duplicate of an earlier one: exact
    duplicates were flagged at ingest and are themed once (docs/02, step 6).
    The related answer is the respondent's own answer to the question's
    related closed question, joined on the same respondent; a multi-select
    is its options joined with commas, and a blank is None."""
    rows = conn.execute(
        """
        SELECT a.id, a.value_text,
               (SELECT string_agg(r.value_text, ', ' ORDER BY r.value_text)
                  FROM answer r
                 WHERE r.respondent_id = a.respondent_id
                   AND r.question_id = q.related_closed_question_id
                   AND NOT r.is_blank) AS related
          FROM answer a JOIN question q ON q.id = a.question_id
         WHERE a.question_id = %s AND NOT a.is_blank AND a.duplicate_of_answer_id IS NULL
         ORDER BY a.id
        """,
        (question_id,),
    ).fetchall()
    return [(int(row["id"]), str(row["value_text"]), row["related"]) for row in rows]


def batches(
    conn: psycopg.Connection[DictRow],
    question_id: UUID,
    *,
    seed: int,
    size: int = BATCH_SIZE,
    token_cap: int = TOKEN_CAP,
) -> list[Batch]:
    """The generation plan: partitioned by related answer, shuffled within
    each partition with the seed, cut at `size` answers or `token_cap`
    tokens, whichever comes first. Deterministic for a seed, so it is
    recomputed on takeover rather than stored."""
    by_related: dict[str | None, list[PromptAnswer]] = {}
    for answer_id, text, related in distinct_answers(conn, question_id):
        by_related.setdefault(related, []).append(PromptAnswer(answer_id, text))
    # A stored seed and Python's own generator: the shuffle is for spread
    # across batches, not for secrecy (docs/02, step 6).
    rng = random.Random(seed)  # noqa: S311
    plan: list[Batch] = []
    for related in sorted(by_related, key=lambda r: (r is None, r or "")):
        answers = by_related[related]
        rng.shuffle(answers)
        chunk: list[PromptAnswer] = []
        tokens = 0
        for answer in answers:
            cost = _tokens(answer.text)
            if chunk and (len(chunk) >= size or tokens + cost > token_cap):
                plan.append(Batch(related, tuple(chunk)))
                chunk, tokens = [], 0
            chunk.append(answer)
            tokens += cost
        if chunk:
            plan.append(Batch(related, tuple(chunk)))
    return plan


@dataclass(frozen=True)
class FindThemesJob:
    """The find_themes job row and the question it is for."""

    job_id: UUID
    question_id: UUID
    consultation_id: UUID
    question_text: str
    model_alias: str
    seed: int


def load_job(conn: psycopg.Connection[DictRow], job_id: UUID) -> FindThemesJob:
    """The job as the runner needs it. The alias and the seed are set on the
    row before the stages run, so a takeover reads the same two."""
    row = conn.execute(
        """
        SELECT j.id, j.question_id, j.consultation_id, j.model_alias, j.params, q.question_text
          FROM job j JOIN question q ON q.id = j.question_id
         WHERE j.id = %s AND j.kind = 'find_themes'
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"job {job_id} is not a find_themes job")
    seed = row["params"].get("seed")
    if row["model_alias"] is None or not isinstance(seed, int):
        raise LookupError(f"job {job_id} has no model alias or seed yet")
    return FindThemesJob(
        job_id=row["id"],
        question_id=row["question_id"],
        consultation_id=row["consultation_id"],
        question_text=row["question_text"],
        model_alias=row["model_alias"],
        seed=seed,
    )


def ensure_version(conn: psycopg.Connection[DictRow], lease: Lease, question_id: UUID) -> UUID:
    """v1, the candidate, created on the job's first batch so every later
    batch has somewhere to put its themes and a takeover finds them. The
    unique index on (question_id, version_no) makes the second call find
    the first's row (docs/04, section 3). Fenced like every worker write."""
    jobs.heartbeat(conn, lease)
    conn.execute(
        """
        INSERT INTO theme_set_version (department_id, question_id, version_no, status)
        SELECT department_id, id, 1, 'candidate' FROM question WHERE id = %s
        ON CONFLICT (question_id, version_no) DO NOTHING
        """,
        (question_id,),
    )
    row = conn.execute(
        "SELECT id FROM theme_set_version WHERE question_id = %s AND version_no = 1", (question_id,)
    ).fetchone()
    if row is None:
        raise LeaseLostError(lease)
    version_id: UUID = row["id"]
    return version_id


def _insert_candidates(
    conn: psycopg.Connection[DictRow], version_id: UUID, themes: Sequence[ProposedTheme]
) -> None:
    # Candidates are the longlist until condensation picks the shortlist.
    # Each key once per version (docs/04, section 3), so a replayed batch
    # writes nothing here either.
    conn.cursor().executemany(
        """
        INSERT INTO theme (department_id, theme_set_version_id, key, label, description, is_longlist)
        SELECT department_id, id, %s, %s, %s, true FROM theme_set_version WHERE id = %s
        ON CONFLICT (theme_set_version_id, key) DO NOTHING
        """,
        [(theme.key, theme.label, theme.description, version_id) for theme in themes],
    )


def generate(
    conn: psycopg.Connection[DictRow],
    llm: LLM,
    lease: Lease,
    job: FindThemesJob,
    version_id: UUID,
    plan: Sequence[Batch],
    *,
    after_batch: Callable[[int], None] | None = None,
) -> int:
    """Step 6's generation: one call per batch, the candidates into v1, a
    checkpoint per batch. Starts at the batch after the last checkpoint, so
    a worker taking over runs only what the last one never finished
    (ADR-002). `after_batch` is where the caller commits. Returns the
    number of batches this call ran."""
    start = jobs.next_batch_no(conn, lease.job_id)
    ran = 0
    for batch_no, batch in enumerate(plan, start=1):
        if batch_no < start:
            continue
        prompt = find_themes_prompt(
            model_alias=job.model_alias,
            question_text=job.question_text,
            related_answer=batch.related_answer,
            answers=batch.answers,
        )
        completion = llm.complete(prompt)
        themes = parse_themes(completion)
        jobs.heartbeat(conn, lease)
        _insert_candidates(conn, version_id, themes)
        jobs.checkpoint(
            conn,
            lease,
            batch_no=batch_no,
            stage="generate",
            answer_ids=[answer.id for answer in batch.answers],
            trace_id=completion.trace_id,
            tokens_in=completion.tokens_in,
            tokens_out=completion.tokens_out,
        )
        ran += 1
        if after_batch is not None:
            after_batch(batch_no)
    return ran
