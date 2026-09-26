"""What the find_themes job promises (docs/02, steps 6 and 7; ADR-002).

Driven stage by stage with the model a fake: batches of distinct answers,
generation, condensation to a capped shortlist with lineage, a preview
that gives every candidate a count and quotes, then v1 and fan-in 1 in one
transaction, with a checkpoint per batch so a takeover resumes rather than
repeats. Every expectation is read from the fixture CSV or the fakes'
script, not from the code under test.
"""

from __future__ import annotations

from collections import Counter

import psycopg
import pytest
from psycopg.rows import DictRow

from consult.ingest import ingest
from consult.themes import BATCH_SIZE, batches
from tests.pipeline import NOT_ANSWERED, fixture_rows, staged_fixture

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
