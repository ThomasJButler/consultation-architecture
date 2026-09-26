"""The find_themes job, stage by stage (docs/02, step 6; ADR-002).

One `job_batch` row is one call of one stage function on one chunk. The
stages here take a connection and never commit; the caller owns the
transaction and commits between batches, which is what makes a checkpoint
worth having. The plan of batches is recomputed from a seed stored on the
job rather than stored itself, so a worker taking over can rebuild it and
start at the batch after the last checkpoint. `run_find_themes` strings
the stages together for a claimed job and ends in the transaction docs/02
step 7 describes: v1 complete, the question themes_ready, fan-in 1 run,
the job succeeded.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from consult import jobs, transitions
from consult.jobs import Lease, LeaseLostError
from consult.llm import LLM
from consult.prompts import PromptAnswer, condense_prompt, find_themes_prompt, map_themes_prompt
from consult.replies import ProposedTheme, parse_assignments, parse_condensation, parse_themes

# About fifty distinct answers a batch (docs/02, step 6), under a token cap
# that keeps a batch of long answers within what docs/05 budgets for a call:
# 150 tokens per open answer at four characters a token, times fifty.
BATCH_SIZE = 50
TOKEN_CAP = 7_500
CHARS_PER_TOKEN = 4
# Condense to about thirty, cap seventy (docs/02, step 6).
SHORTLIST_CAP = 70
# The preview: a stratified sample of 200 answers mapped in batches of ten,
# the mapping shape (docs/02, steps 6 and 9), and three quotes a theme for
# the sign-off screen (docs/04, theme_example).
SAMPLE_SIZE = 200
PREVIEW_BATCH_SIZE = 10
EXAMPLES_PER_THEME = 3


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
    rng = random.Random(seed)  # spread, not secrecy  # noqa: S311  # nosec B311
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


def _candidates(conn: psycopg.Connection[DictRow], version_id: UUID) -> list[ProposedTheme]:
    rows = conn.execute(
        """
        SELECT key, label, description FROM theme
         WHERE theme_set_version_id = %s AND is_longlist ORDER BY key
        """,
        (version_id,),
    ).fetchall()
    return [ProposedTheme(r["key"], r["label"], r["description"] or "") for r in rows]


def _shortlist_size(conn: psycopg.Connection[DictRow], version_id: UUID) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM theme WHERE theme_set_version_id = %s AND NOT is_longlist",
        (version_id,),
    ).fetchone()
    return int(row["n"]) if row else 0


def condense(
    conn: psycopg.Connection[DictRow],
    llm: LLM,
    lease: Lease,
    job: FindThemesJob,
    version_id: UUID,
    *,
    generated: int,
    cap: int = SHORTLIST_CAP,
) -> int:
    """Step 6's condensation: one call over every candidate, the shortlist
    written into v1 with each longlist row pointing at the theme it folded
    into. Themes past the cap (most merges first, ties by key) are not
    written and their candidates keep their rows with no lineage. One
    checkpoint, after the generation batches; a second call finds it and
    does nothing. Returns the shortlist's size."""
    done = conn.execute(
        "SELECT 1 FROM job_batch WHERE job_id = %s AND stage = 'condense'", (lease.job_id,)
    ).fetchone()
    if done is not None:
        return _shortlist_size(conn, version_id)
    batch_no = jobs.next_batch_no(conn, lease.job_id)
    if batch_no != generated + 1:
        raise LookupError(
            f"job {lease.job_id}: generation has {batch_no - 1} of {generated} batches"
        )
    candidates = _candidates(conn, version_id)
    prompt = condense_prompt(
        model_alias=job.model_alias, question_text=job.question_text, candidates=candidates
    )
    completion = llm.complete(prompt)
    condensed = parse_condensation(completion, [c.key for c in candidates])
    shortlist = sorted(condensed, key=lambda t: (-len(t.merges), t.key))[:cap]
    jobs.heartbeat(conn, lease)
    for theme in shortlist:
        # A folded theme that kept a candidate's own key is that candidate,
        # promoted; anything else is a new row. Either way the key is once
        # per version (docs/04, section 3).
        row = conn.execute(
            """
            INSERT INTO theme (department_id, theme_set_version_id, key, label, description,
                               is_longlist)
            SELECT department_id, id, %(key)s, %(label)s, %(description)s, false
              FROM theme_set_version WHERE id = %(version)s
            ON CONFLICT (theme_set_version_id, key) DO UPDATE
               SET label = EXCLUDED.label, description = EXCLUDED.description,
                   is_longlist = false, lineage_theme_id = NULL
            RETURNING id
            """,
            {
                "key": theme.key,
                "label": theme.label,
                "description": theme.description,
                "version": version_id,
            },
        ).fetchone()
        if row is None:
            raise LeaseLostError(lease)
        conn.execute(
            """
            UPDATE theme SET lineage_theme_id = %(theme)s
             WHERE theme_set_version_id = %(version)s AND is_longlist
               AND key = ANY(%(merged)s) AND key <> %(key)s
            """,
            {
                "theme": row["id"],
                "version": version_id,
                "merged": list(theme.merges),
                "key": theme.key,
            },
        )
    jobs.checkpoint(
        conn,
        lease,
        batch_no=batch_no,
        stage="condense",
        answer_ids=[],
        trace_id=completion.trace_id,
        tokens_in=completion.tokens_in,
        tokens_out=completion.tokens_out,
    )
    return len(shortlist)


def stratified_sample(
    strata: dict[str | None, list[PromptAnswer]], *, size: int, seed: int
) -> dict[str | None, list[PromptAnswer]]:
    """Up to `size` answers, allocated to each stratum in proportion to its
    share, every non-empty stratum getting at least one while the size
    allows, drawn with the seed so a takeover draws the same sample.
    Everyone, in stratum order, when the size covers them all."""
    total = sum(len(answers) for answers in strata.values())
    if total <= size:
        return {related: list(answers) for related, answers in strata.items()}
    ordered = sorted(strata, key=lambda r: (r is None, r or ""))
    # Largest-remainder allocation: floors first, then the leftovers to the
    # strata with the largest fractional parts, so the shares add up.
    shares = {r: len(strata[r]) * size / total for r in ordered}
    counts = {r: max(1, int(shares[r])) if strata[r] else 0 for r in ordered}
    leftover = size - sum(counts.values())
    for related in sorted(ordered, key=lambda r: shares[r] - int(shares[r]), reverse=True):
        if leftover <= 0:
            break
        if counts[related] < len(strata[related]):
            counts[related] += 1
            leftover -= 1
    while leftover < 0:
        biggest = max(ordered, key=lambda r: counts[r])
        counts[biggest] -= 1
        leftover += 1
    rng = random.Random(seed)  # spread, not secrecy  # noqa: S311  # nosec B311
    sample: dict[str | None, list[PromptAnswer]] = {}
    for related in ordered:
        answers = list(strata[related])
        rng.shuffle(answers)
        sample[related] = sorted(answers[: counts[related]], key=lambda a: a.id)
    return sample


def _shortlist(conn: psycopg.Connection[DictRow], version_id: UUID) -> list[DictRow]:
    return conn.execute(
        """
        SELECT id, key, label, description FROM theme
         WHERE theme_set_version_id = %s AND NOT is_longlist ORDER BY key
        """,
        (version_id,),
    ).fetchall()


def preview(
    conn: psycopg.Connection[DictRow],
    llm: LLM,
    lease: Lease,
    job: FindThemesJob,
    version_id: UUID,
    *,
    generated: int,
    sample_size: int = SAMPLE_SIZE,
    after_batch: Callable[[int], None] | None = None,
) -> int:
    """Step 6's preview: the shortlist mapped over a stratified sample in
    batches of ten, so every candidate gets a count and quotes before a
    reviewer sees it. Batches are numbered on from the condense checkpoint
    and skipped when already checkpointed, and the counts are added per
    batch in the same transaction as its checkpoint, so a takeover can't
    count a batch twice. Returns the answers previewed by this call."""
    strata: dict[str | None, list[PromptAnswer]] = {}
    for answer_id, text, related in distinct_answers(conn, job.question_id):
        strata.setdefault(related, []).append(PromptAnswer(answer_id, text))
    sample = stratified_sample(strata, size=sample_size, seed=job.seed)
    shortlist = _shortlist(conn, version_id)
    themes = [(str(t["key"]), str(t["label"]), t["description"]) for t in shortlist]
    theme_ids = {str(t["key"]): t["id"] for t in shortlist}
    start = jobs.next_batch_no(conn, lease.job_id)
    # Every shortlist theme gets a count before any batch runs, zero
    # included, so the screen reads "0" and not "unknown" for a theme the
    # sample never picks. Idempotent, so a takeover doing it again is fine.
    jobs.heartbeat(conn, lease)
    conn.execute(
        """
        UPDATE theme SET preview_count = coalesce(preview_count, 0)
         WHERE theme_set_version_id = %s AND NOT is_longlist
        """,
        (version_id,),
    )
    batch_no = generated + 1  # the condense checkpoint
    previewed = 0
    for related in sorted(sample, key=lambda r: (r is None, r or "")):
        answers = sample[related]
        for offset in range(0, len(answers), PREVIEW_BATCH_SIZE):
            batch_no += 1
            chunk = answers[offset : offset + PREVIEW_BATCH_SIZE]
            if batch_no < start:
                continue
            prompt = map_themes_prompt(
                model_alias=job.model_alias,
                question_text=job.question_text,
                related_answer=related,
                themes=themes,
                answers=chunk,
            )
            completion = llm.complete(prompt)
            assignments = parse_assignments(completion, prompt)
            jobs.heartbeat(conn, lease)
            for assignment in assignments:
                for key in assignment.theme_keys:
                    _count_and_quote(conn, theme_ids[key], assignment.answer_id)
            jobs.checkpoint(
                conn,
                lease,
                batch_no=batch_no,
                stage="preview",
                answer_ids=[a.id for a in chunk],
                trace_id=completion.trace_id,
                tokens_in=completion.tokens_in,
                tokens_out=completion.tokens_out,
            )
            previewed += len(chunk)
            if after_batch is not None:
                after_batch(batch_no)
    return previewed


def _count_and_quote(conn: psycopg.Connection[DictRow], theme_id: UUID, answer_id: int) -> None:
    conn.execute(
        "UPDATE theme SET preview_count = coalesce(preview_count, 0) + 1 WHERE id = %s", (theme_id,)
    )
    # Up to three quotes a theme, ranked in the order the preview met them;
    # one row per answer per theme however often a batch is replayed.
    conn.execute(
        """
        INSERT INTO theme_example (department_id, theme_id, answer_id, rank)
        SELECT t.department_id, t.id, %(answer)s,
               (SELECT count(*) + 1 FROM theme_example WHERE theme_id = t.id)
          FROM theme t
         WHERE t.id = %(theme)s
           AND (SELECT count(*) FROM theme_example WHERE theme_id = t.id) < %(cap)s
        ON CONFLICT (theme_id, answer_id) DO NOTHING
        """,
        {"answer": answer_id, "theme": theme_id, "cap": EXAMPLES_PER_THEME},
    )


def run_find_themes(
    conn: psycopg.Connection[DictRow],
    llm: LLM,
    lease: Lease,
    *,
    after_batch: Callable[[int], None] | None = None,
) -> transitions.Advance:
    """The whole job for a claimed lease: the question moved on (or found
    already moved by a takeover), v1 ensured, the plan rebuilt from the
    seed, then generation, condensation and preview, each skipping what a
    checkpoint already covers, then the finishing transaction. The caller
    commits in `after_batch`; nothing here does."""
    job = load_job(conn, lease.job_id)
    transitions.start_find_themes(conn, job.question_id)
    version_id = ensure_version(conn, lease, job.question_id)
    plan = batches(conn, job.question_id, seed=job.seed)
    generate(conn, llm, lease, job, version_id, plan, after_batch=after_batch)
    condense(conn, llm, lease, job, version_id, generated=len(plan))
    if after_batch is not None:
        after_batch(len(plan) + 1)
    preview(conn, llm, lease, job, version_id, generated=len(plan), after_batch=after_batch)
    return transitions.finish_find_themes(conn, lease, job.question_id, job.consultation_id)
