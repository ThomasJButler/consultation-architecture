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
from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from consult.prompts import PromptAnswer

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
