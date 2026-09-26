"""The fourth mechanic docs/02 section 13 names, the indexed filter query,
proved on a plan rather than asserted: ADR-004's acceptance test, as
docs/04 section 6 and docs/05 section 9 describe it.

A 20,000-respondent consultation from the seeded generator (`--scale`),
staged, configured and ingested the way the 240-row fixture is, both
open questions signed off by hand with factory tags (the benchmark
measures the plan, not mapping), ANALYZE, then `EXPLAIN (ANALYZE,
FORMAT JSON)` on the three-predicate filter's first page. The JSON plan
tree is walked for the two nodes the design names: a Bitmap Index Scan
on `respondent_attrs_gin` and an Index Scan on `answer_question_id_id`.

The selectivity and each node's actual rows are measured here, so the
test prints them (CLAUDE.md rule 11). Nothing printed is answer text.
The 500 ms at ten million answers stays with the staging load test
(ADR-007).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from consult.ingest import ingest
from consult.query import parse_filters, scope
from make_fixture_data import write_fixtures
from tests.pipeline import staged_fixture
from tests.rows import make_theme_set_version, tag_answers_by_rule

pytestmark = pytest.mark.db

RESPONDENTS = 20_000
# docs/02 step 11's three predicate kinds, in the grammar a URL carries.
FILTER = ("attr:d_area=Villages", "theme:PARKING", "other:o_safety.theme=LIGHTING")
# docs/04 section 6, "Responses, one page", the first page: ADR-004's
# filter query with ORDER BY a.id LIMIT 20 OFFSET.
FIRST_PAGE = sql.SQL(
    "SELECT id, value_text, duplicate_of_answer_id FROM scope ORDER BY id LIMIT 20 OFFSET 0"
)


def _o_reason_key(text: str) -> str:
    # The split test_query_db.py hand-counts on the 240 rows: two real
    # Oppose reasons in REASONS (scripts/make_fixture_data.py), the rest OTHER.
    lowered = text.casefold()
    if "parking" in lowered:
        return "PARKING"
    if "junction" in lowered:
        return "SAFETY"
    return "OTHER"


def _o_safety_key(text: str) -> str:
    # One of the eight SAFETY fragments (scripts/make_fixture_data.py).
    return "LIGHTING" if "lighting" in text.casefold() else "OTHER"


def _nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every node in an EXPLAIN (FORMAT JSON) tree: children, InitPlans and
    SubPlans all hang under "Plans"."""
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


@pytest.mark.slow
def test_the_filter_plan_uses_both_indexes_at_twenty_thousand(
    db: psycopg.Connection[DictRow], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    written = write_fixtures(tmp_path, respondents=RESPONDENTS)
    started = time.monotonic()
    staged = staged_fixture(db, path=written.responses)
    ingested = ingest(db, staged.consultation_id)
    load_seconds = time.monotonic() - started
    assert ingested.respondents == RESPONDENTS

    # Signed off by hand: a signed-off version per open question and
    # factory tags against it (tests/rows.py). scope reads each
    # question's latest signed-off version (consult/query.py).
    reason = staged.configured.questions["o_reason"]
    safety = staged.configured.questions["o_safety"]
    for question_id, key_for in ((reason, _o_reason_key), (safety, _o_safety_key)):
        version_id = make_theme_set_version(db, question_id, status="signed_off")
        tag_answers_by_rule(db, version_id, question_id, key_for)
    db.execute("ANALYZE respondent, answer, answer_theme")

    shares = db.execute(
        """
        SELECT count(*) FILTER (WHERE attrs @> %s) AS villages, count(*) AS everyone
          FROM respondent WHERE consultation_id = %s
        """,
        (Jsonb({"d_area": ["Villages"]}), staged.consultation_id),
    ).fetchone()
    assert shares is not None
    selectivity = shares["villages"] / shares["everyone"]

    compiled = scope(reason, parse_filters(FILTER))
    explain = sql.SQL("EXPLAIN (ANALYZE, FORMAT JSON) ") + compiled.sql + FIRST_PAGE
    row = db.execute(explain, compiled.params).fetchone()
    assert row is not None
    nodes = list(_nodes(row["QUERY PLAN"][0]["Plan"]))

    gin = [
        node
        for node in nodes
        if node["Node Type"] == "Bitmap Index Scan"
        and node.get("Index Name") == "respondent_attrs_gin"
    ]
    per_question = [
        node
        for node in nodes
        if node["Node Type"] in ("Index Scan", "Index Only Scan")
        and node.get("Index Name") == "answer_question_id_id"
    ]
    scanned = sorted(
        {
            f"{node['Node Type']} on {node.get('Index Name', node.get('Relation Name'))}"
            for node in nodes
            if "Scan" in node["Node Type"]
        }
    )

    # Measured here (CLAUDE.md rule 11): ids, counts and durations only.
    with capsys.disabled():
        print(  # noqa: T201
            f"\nplan benchmark: respondents={shares['everyone']} villages={shares['villages']}"
            f" selectivity={selectivity:.4f} load_seconds={load_seconds:.1f}"
            f" gin_actual_rows={[node['Actual Rows'] for node in gin]}"
            f" per_question_actual_rows={[node['Actual Rows'] for node in per_question]}"
            f"\nplan benchmark: scans={scanned}"
        )

    assert gin, scanned
    assert per_question, scanned
