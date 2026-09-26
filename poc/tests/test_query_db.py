"""What the scope CTE promises (docs/04 section 6): one statement whose
text is fixed by the filter's shape, every value a parameter (docs/06
section 2, parameterised SQL only; ADR-004), so a filter value typed into
the address bar can't reach the SQL text (THREAT_MODEL.md row 5).

Marked `db`: the builder is run against Postgres here. The grammar's own
test is `test_query.py`, which stays pure.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import DictRow

from consult import store
from consult.ingest import ingest
from consult.query import AttrFilter, Filter, OtherFilter, related_distribution, scope, theme_table
from make_fixture_data import PROFORMA_REASON, PROFORMA_ROWS
from tests.pipeline import fixture_rows, signed_off_fixture, signed_off_questions, staged_fixture
from tests.rows import make_department, tag_answers_by_rule
from tests.test_themes import distinct_reasons

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


def _department_of(db: psycopg.Connection[DictRow], question_id: UUID) -> UUID:
    row = db.execute("SELECT department_id FROM question WHERE id = %s", (question_id,)).fetchone()
    assert row is not None
    department_id: UUID = row["department_id"]
    return department_id


def _run(db: psycopg.Connection[DictRow], question_id: UUID, wanted: Filter) -> list[DictRow]:
    # The question's own department: these tests are about the filter.
    compiled = scope(question_id, wanted, department_id=_department_of(db, question_id))
    query = compiled.sql + sql.SQL("SELECT id FROM scope ORDER BY id")
    return db.execute(query, compiled.params).fetchall()


def _text(
    db: psycopg.Connection[DictRow], question_id: UUID, department_id: UUID, wanted: Filter
) -> str:
    return scope(question_id, wanted, department_id=department_id).sql.as_string(db)


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
    department_id = _department_of(db, question_id)

    # The text is fixed by the shape (one attr, one theme, one other) and
    # by nothing a user typed, the question and department ids included.
    expected = _text(db, question_id, department_id, _one_of_each("benign"))
    for value in (*HOSTILE, NUL):
        assert _text(db, question_id, department_id, _one_of_each(value)) == expected
        assert _text(db, uuid4(), uuid4(), _one_of_each(value)) == expected

    # Every placeholder in the text has a parameter and every parameter a
    # placeholder, whatever the shape. The question and department ids are
    # the only values an empty filter carries, and a second attr adds one
    # placeholder.
    two_attrs = Filter(
        attrs=(AttrFilter("d_area", "Villages"), AttrFilter("c_route", "Oppose")),
        themes=("benign",),
        others=(OtherFilter("o_safety", "benign"),),
    )
    shapes = [Filter(), _one_of_each("benign"), two_attrs, *_each_alone("benign")]
    for shape in shapes:
        compiled = scope(question_id, shape, department_id=department_id)
        assert set(PLACEHOLDER.findall(compiled.sql.as_string(db))) == set(compiled.params)
    assert len(scope(question_id, Filter(), department_id=department_id).params) == 2
    one_attr = scope(question_id, _one_of_each("benign"), department_id=department_id)
    two = scope(question_id, two_attrs, department_id=department_id)
    assert len(two.params) == len(one_attr.params) + 1
    assert _text(db, question_id, department_id, two_attrs) != expected

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


def _o_reason_key(text: str) -> str:
    # Both fragments name a real Oppose reason in REASONS
    # (scripts/make_fixture_data.py): "We'd lose the only parking near the
    # surgery on Mill Lane" and "The junction by the bridge is dangerous
    # already", so PARKING and SAFETY each get more than one respondent to
    # hand-count from responses.csv.
    lowered = text.casefold()
    if "parking" in lowered:
        return "PARKING"
    if "junction" in lowered:
        return "SAFETY"
    return "OTHER"


def test_the_theme_table_counts_match_the_fixture(db: psycopg.Connection[DictRow]) -> None:
    signed = signed_off_fixture(db)
    tag_answers_by_rule(db, signed.version_id, signed.question_id, _o_reason_key)

    # Hand count from responses.csv: distinct, non-blank o_reason answers,
    # duplicates hidden (CLAUDE.md rule 2, docs/04 section 6's denominator).
    department_id = _department_of(db, signed.question_id)
    everyone = theme_table(db, signed.question_id, Filter(), department_id=department_id)
    assert everyone.denominator == 74
    assert {(c.key, c.respondents) for c in everyone.rows} == {
        ("OTHER", 46),
        ("PARKING", 17),
        ("SAFETY", 11),
    }
    # Ordered by count descending, then key.
    assert [c.key for c in everyone.rows] == ["OTHER", "PARKING", "SAFETY"]
    assert {c.key: c.label for c in everyone.rows} == {
        "OTHER": "Other",
        "PARKING": "Parking",
        "SAFETY": "Safety",
    }
    # OTHER is the fallback theme_set_version.sign_off already writes
    # (transitions.FALLBACK_THEMES); PARKING and SAFETY are this factory's.
    assert {c.key: c.is_fallback for c in everyone.rows} == {
        "OTHER": True,
        "PARKING": False,
        "SAFETY": False,
    }

    villages = theme_table(
        db,
        signed.question_id,
        Filter(attrs=(AttrFilter("d_area", "Villages"),)),
        department_id=department_id,
    )
    assert villages.denominator == 17
    assert {(c.key, c.respondents) for c in villages.rows} == {
        ("OTHER", 12),
        ("PARKING", 4),
        ("SAFETY", 1),
    }

    # Two values in one column are OR'd (docs/04 section 5): Villages or
    # Suburbs, no test reaches this branch's exact counts yet.
    either_area = theme_table(
        db,
        signed.question_id,
        Filter(attrs=(AttrFilter("d_area", "Villages"), AttrFilter("d_area", "Suburbs"))),
        department_id=department_id,
    )
    assert either_area.denominator == 42
    assert {(c.key, c.respondents) for c in either_area.rows} == {
        ("OTHER", 25),
        ("PARKING", 12),
        ("SAFETY", 5),
    }

    # The related closed question, c_route, among the Villages respondents
    # counted above: docs/02 screen 4's own second panel.
    distribution = related_distribution(
        db,
        signed.question_id,
        Filter(attrs=(AttrFilter("d_area", "Villages"),)),
        department_id=department_id,
    )
    assert distribution == [("Support", 8), ("Oppose", 4), ("Not sure", 4)]


def _o_safety_key(text: str) -> str:
    # "Proper lighting after dark along the towpath" is one of the eight
    # SAFETY fragments (scripts/make_fixture_data.py); the rest give OTHER.
    return "LIGHTING" if "lighting" in text.casefold() else "OTHER"


def test_the_other_filter_is_a_semi_join_across_questions(
    db: psycopg.Connection[DictRow],
) -> None:
    signed = signed_off_questions(db, ("o_reason", "o_safety"))
    tag_answers_by_rule(
        db, signed["o_safety"].version_id, signed["o_safety"].question_id, _o_safety_key
    )

    # Hand count from responses.csv: canonical, non-blank o_reason rows
    # whose respondent's own o_safety answer contains "lighting" (screen
    # 4's own other: example, on a different pair of questions and keys).
    narrowed = _run(
        db,
        signed["o_reason"].question_id,
        Filter(others=(OtherFilter("o_safety", "LIGHTING"),)),
    )
    assert len(narrowed) == 15


def _proforma_key(text: str) -> str:
    # Isolates the proforma's own group from every other duplicate group
    # in o_reason, so its count is the twelve copies and nothing else.
    return "PROFORMA" if text == PROFORMA_REASON else "GENERAL"


def test_duplicates_are_hidden_unless_asked_for(db: psycopg.Connection[DictRow]) -> None:
    assert len(PROFORMA_ROWS) == 12
    signed = signed_off_fixture(db)
    tag_answers_by_rule(db, signed.version_id, signed.question_id, _proforma_key)

    # Ingest flags eleven of the proforma's twelve copies as duplicates of
    # the twelfth (docs/02 section 7, decision 9; PROFORMA_ROWS in
    # scripts/make_fixture_data.py), so with the toggle off the denominator
    # counts one of them, and the PROFORMA key with it.
    department_id = _department_of(db, signed.question_id)
    hidden = theme_table(db, signed.question_id, Filter(), department_id=department_id)
    assert hidden.denominator == 74
    proforma_hidden = {c.key: c.respondents for c in hidden.rows}["PROFORMA"]
    assert proforma_hidden == 1

    # with=duplicates drops both IS NULL predicates, so every physical row
    # this factory tagged counts, the eleven copies included.
    shown = theme_table(
        db, signed.question_id, Filter(with_duplicates=True), department_id=department_id
    )
    assert shown.denominator == 221
    proforma_shown = {c.key: c.respondents for c in shown.rows}["PROFORMA"]
    assert proforma_shown == 12
    assert proforma_shown - proforma_hidden == 11


def test_the_scope_holds_to_the_callers_department(db: psycopg.Connection[DictRow]) -> None:
    signed = signed_off_fixture(db)
    tag_answers_by_rule(db, signed.version_id, signed.question_id, _o_reason_key)
    other_department = make_department(db)

    # A question id is a guessable value: named by a caller from another
    # department, it reaches no row (docs/06 section 2, the department is
    # the caller's; THREAT_MODEL.md row 5).
    guessed = theme_table(db, signed.question_id, Filter(), department_id=other_department)
    assert guessed.denominator == 0
    assert guessed.rows == []
    assert (
        related_distribution(db, signed.question_id, Filter(), department_id=other_department) == []
    )

    # The question's own department still gets the hand count from
    # responses.csv that test_the_theme_table_counts_match_the_fixture pins.
    own = theme_table(
        db, signed.question_id, Filter(), department_id=_department_of(db, signed.question_id)
    )
    assert own.denominator == 74


def test_the_related_lookup_stays_inside_the_department(db: psycopg.Connection[DictRow]) -> None:
    # The related distribution first finds the question's related closed
    # question, and that lookup is a read like any other: held to the
    # caller's department (docs/06 section 2), since a question id is a
    # guessable value (THREAT_MODEL.md row 5). From another department, a
    # question that exists and an id nobody has must get the same answer,
    # or the difference says which ids exist elsewhere. The answer is the
    # empty distribution test_the_scope_holds_to_the_callers_department
    # pins for the first.
    signed = signed_off_fixture(db)
    other_department = make_department(db)

    def asked(question_id: UUID) -> object:
        try:
            return related_distribution(db, question_id, Filter(), department_id=other_department)
        except LookupError:
            return LookupError

    assert asked(signed.question_id) == asked(uuid4()) == []


# Lines 2 to 41 of responses.csv, its first forty respondents.
NA_LINES = range(2, 42)


def test_a_kept_na_counts_in_the_related_distribution(
    db: psycopg.Connection[DictRow], tmp_path: Path
) -> None:
    # docs/02 section 3.2 makes N/A on a closed column a real value, kept
    # unless the reviewer says otherwise, and ingest stores it with no
    # option: value_text 'N/A', option_id NULL. The related distribution
    # has to count it as it counts an option, or everyone who gave it
    # drops out of screen 4's second panel while attr:c_route=N/A still
    # finds them. c_route is N/A on NA_LINES here, under the default
    # resolutions. The hand count runs over distinct_reasons(), the first
    # occurrence of each o_reason text, which is the scope with duplicates
    # hidden: a duplicate respondent repeats an earlier o_reason. The order
    # is the definition's options, then N/A.
    rows = fixture_rows()
    for line, row in enumerate(rows, start=2):
        if line in NA_LINES:
            row["c_route"] = "N/A"
    path = tmp_path / "responses.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    staged = staged_fixture(db, path=path)
    ingest(db, staged.consultation_id)
    question_id = staged.configured.questions["o_reason"]

    answered = Counter(
        "N/A" if line in NA_LINES else related
        for line, _text, related in distinct_reasons()
        if line in NA_LINES or related is not None
    )
    expected = [(label, answered[label]) for label in ("Support", "Oppose", "Not sure", "N/A")]

    distribution = related_distribution(
        db, question_id, Filter(), department_id=_department_of(db, question_id)
    )

    assert distribution == expected
    assert sum(count for _label, count in distribution) == sum(answered.values())


def test_the_theme_table_is_one_statement(
    db: psycopg.Connection[DictRow], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A READ COMMITTED transaction takes a new snapshot for each statement
    (Postgres documentation, 13.2.1), so a denominator counted in one
    statement and the themes in the next can see different tags: a tag
    committed between the two gives a count above its denominator. One
    statement is one snapshot, and docs/04 section 6 puts the denominator
    in the same statement as the counts."""
    signed = signed_off_fixture(db)
    department_id = _department_of(db, signed.question_id)
    statements: list[object] = []
    original = db.execute

    def counting(*args: Any, **kwargs: Any) -> Any:
        statements.append(args[0])
        return original(*args, **kwargs)

    # Nothing tagged yet: the denominator still comes back, with no rows.
    with monkeypatch.context() as patch:
        patch.setattr(db, "execute", counting)
        untagged = theme_table(db, signed.question_id, Filter(), department_id=department_id)
    assert len(statements) == 1
    assert untagged.denominator == 74
    assert untagged.rows == []

    tag_answers_by_rule(db, signed.version_id, signed.question_id, _o_reason_key)
    statements.clear()
    with monkeypatch.context() as patch:
        patch.setattr(db, "execute", counting)
        tagged = theme_table(
            db, signed.question_id, Filter(themes=("PARKING",)), department_id=department_id
        )
    assert len(statements) == 1
    assert tagged.denominator == 17
    assert [(c.key, c.respondents) for c in tagged.rows] == [("PARKING", 17)]


def test_the_scope_holds_only_an_open_question(db: psycopg.Connection[DictRow]) -> None:
    signed = signed_off_fixture(db)
    department_id = _department_of(db, signed.question_id)
    row = db.execute(
        "SELECT id FROM question WHERE consultation_id = %s AND column_ref = 'c_modes'",
        (signed.consultation_id,),
    ).fetchone()
    assert row is not None

    # c_modes is a multi-select closed question: one answer row per option
    # ticked, so its rows aren't respondents, and docs/04 section 6's
    # denominator is "one non-blank row per respondent" of an open one.
    # The scope of a closed question is empty.
    closed = theme_table(db, row["id"], Filter(), department_id=department_id)
    assert closed.denominator == 0
    assert closed.rows == []
    assert _run(db, row["id"], Filter()) == []
