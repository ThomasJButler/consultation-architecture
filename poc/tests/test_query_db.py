"""What the scope CTE promises (docs/04 section 6): one statement whose
text is fixed by the filter's shape, every value a parameter (docs/06
section 2, parameterised SQL only; ADR-004), so a filter value typed into
the address bar can't reach the SQL text (THREAT_MODEL.md row 5).

Marked `db`: the builder is run against Postgres here. The grammar's own
test is `test_query.py`, which stays pure.
"""

from __future__ import annotations

import re
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import DictRow

from consult import store
from consult.query import AttrFilter, Filter, OtherFilter, scope
from tests.pipeline import signed_off_fixture

pytestmark = pytest.mark.db

# What a user could type to break out of a literal, a placeholder or a
# statement: both quotes, a bare percent and psycopg's own placeholder, a
# statement separator, both comment markers, a tautology that would match
# every row if it reached the text, a value far longer than any key, and
# the classic.
HOSTILE = (
    "'",
    '"',
    "%",
    "%s",
    ";",
    "--",
    "/*",
    "' OR '1'='1",
    "x" * 10_000,
    "'; DROP TABLE answer; --",
)

# Postgres text can't hold a NUL (0x00): psycopg refuses one in a text
# parameter before sending it, and the server refuses `\u0000` in jsonb.
NUL = "\x00"

PLACEHOLDER = re.compile(r"%\((\w+)\)s")


def _one_of_each(value: str) -> Filter:
    return Filter(
        attrs=(AttrFilter(value, value),),
        themes=(value,),
        others=(OtherFilter(value, value),),
    )


def _each_alone(value: str) -> list[Filter]:
    """The value in every slot the grammar has, one predicate at a time,
    then all three kinds together."""
    return [
        Filter(attrs=(AttrFilter("d_area", value),)),
        Filter(attrs=(AttrFilter(value, "Villages"),)),
        Filter(themes=(value,)),
        Filter(others=(OtherFilter(value, "lighting_after_dark"),)),
        Filter(others=(OtherFilter("o_safety", value),)),
        _one_of_each(value),
    ]


def _run(db: psycopg.Connection[DictRow], question_id: UUID, wanted: Filter) -> list[DictRow]:
    compiled = scope(question_id, wanted)
    query = compiled.sql + sql.SQL("SELECT id FROM scope ORDER BY id")
    return db.execute(query, compiled.params).fetchall()


def _text(db: psycopg.Connection[DictRow], question_id: UUID, wanted: Filter) -> str:
    return scope(question_id, wanted).sql.as_string(db)


def _row_counts(db: psycopg.Connection[DictRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in store.TABLES:
        row = db.execute(
            sql.SQL("SELECT count(*) AS n FROM {}").format(store.qualified(name))
        ).fetchone()
        assert row is not None
        counts[name] = row["n"]
    return counts


def test_hostile_filter_values_stay_parameters(db: psycopg.Connection[DictRow]) -> None:
    signed = signed_off_fixture(db)
    question_id = signed.question_id

    # The text is fixed by the shape (one attr, one theme, one other) and
    # by nothing a user typed, the question id included.
    expected = _text(db, question_id, _one_of_each("benign"))
    for value in (*HOSTILE, NUL):
        assert _text(db, question_id, _one_of_each(value)) == expected
        assert _text(db, uuid4(), _one_of_each(value)) == expected

    # Every placeholder in the text has a parameter and every parameter a
    # placeholder, whatever the shape. The question id is the only value
    # an empty filter carries, and a second attr adds one placeholder.
    two_attrs = Filter(
        attrs=(AttrFilter("d_area", "Villages"), AttrFilter("c_route", "Oppose")),
        themes=("benign",),
        others=(OtherFilter("o_safety", "benign"),),
    )
    shapes = [Filter(), _one_of_each("benign"), two_attrs, *_each_alone("benign")]
    for shape in shapes:
        compiled = scope(question_id, shape)
        assert set(PLACEHOLDER.findall(compiled.sql.as_string(db))) == set(compiled.params)
    assert len(scope(question_id, Filter()).params) == 1
    one_attr_count = len(scope(question_id, _one_of_each("benign")).params)
    assert len(scope(question_id, two_attrs).params) == one_attr_count + 1
    assert _text(db, question_id, two_attrs) != expected

    # The query runs and finds rows when the filter allows them, so "none"
    # below is the hostile value failing to match and not a broken query:
    # 53 of the fixture's 240 rows give d_area as Villages (responses.csv).
    everyone = _run(db, question_id, Filter())
    villages = _run(db, question_id, Filter(attrs=(AttrFilter("d_area", "Villages"),)))
    assert everyone
    assert villages
    assert {row["id"] for row in villages} < {row["id"] for row in everyone}

    before = _row_counts(db)
    assert len(before) == 14

    for value in HOSTILE:
        for wanted in _each_alone(value):
            assert _run(db, question_id, wanted) == []

    # A NUL is refused before it runs. The savepoint keeps the fixture's
    # transaction usable after the server's refusal of `\u0000` in jsonb.
    for wanted in _each_alone(NUL):
        with pytest.raises((ValueError, psycopg.DataError)), db.transaction():
            _run(db, question_id, wanted)

    # The schema is untouched: the fourteen tables are all there, with the
    # rows they had before the hostile filters ran.
    assert _row_counts(db) == before
