"""The per-question dashboard's filter grammar (docs/02 step 11, screen 4),
parsed to a typed value before anything touches the scope CTE (docs/04
section 6). The value is typed first so the builder that composes that CTE
never sees a raw query-string fragment, only `AttrFilter`, `OtherFilter`
and theme keys it can bind as `psycopg.sql` parameters: there is no path
from the grammar to SQL text.

Screen 4 gives the four forms, from a query string such as
`?f=attr:d_area=Villages&f=other:o_safety.theme=lighting_after_dark`:
`attr:<column>=<value>` (containment on `respondent.attrs`), `theme:<key>`
repeated and OR'd on this question, `other:<question>.theme=<key>` as a
semi-join onto another question's tags, and `with=duplicates`, a toggle
that is off unless present. Anything else is refused by `FilterCode`,
never by the value that triggered it: a filter value is whatever a user
typed into the address bar (CLAUDE.md rule 8).

`scope` compiles the parsed value to docs/04 section 6's CTE with
`psycopg.sql`. The fixed fragments are `sql.SQL`, every value is a named
placeholder, and no identifier comes from the user: an `attr:` column is
a key inside a jsonb parameter, a theme key and an `other:` question are
compared as text parameters (docs/06 section 2; ADR-004).
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb

_ATTR_PREFIX = "attr:"
_THEME_PREFIX = "theme:"
_OTHER_PREFIX = "other:"
_OTHER_THEME_MARKER = ".theme="
_DUPLICATES_ITEM = "with=duplicates"


class FilterCode(StrEnum):
    UNKNOWN_KIND = "unknown_kind"
    EMPTY_VALUE = "empty_value"
    MALFORMED_OTHER = "malformed_other"


class FilterError(ValueError):
    """A refusal from the grammar. `code` names which rule failed; the
    message carries only the code, since the value that failed it could be
    anything a user typed (CLAUDE.md rule 8).
    """

    def __init__(self, code: FilterCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True)
class AttrFilter:
    column: str
    value: str


@dataclass(frozen=True)
class OtherFilter:
    question: str  # a question.column_ref (docs/04 section 6), not an id
    key: str


@dataclass(frozen=True)
class Filter:
    attrs: tuple[AttrFilter, ...] = ()
    themes: tuple[str, ...] = ()  # OR'd theme keys on this question
    others: tuple[OtherFilter, ...] = ()
    with_duplicates: bool = False


def parse_filters(items: Sequence[str]) -> Filter:
    """Parse one `f=` query-string item per element of `items`, in order.
    An empty sequence is a filter with nothing set: no scope narrowing.
    """
    attrs: list[AttrFilter] = []
    themes: list[str] = []
    others: list[OtherFilter] = []
    with_duplicates = False
    for item in items:
        if item == _DUPLICATES_ITEM:
            with_duplicates = True
        elif item.startswith(_ATTR_PREFIX):
            attrs.append(_parse_attr(item.removeprefix(_ATTR_PREFIX)))
        elif item.startswith(_THEME_PREFIX):
            themes.append(_parse_theme(item.removeprefix(_THEME_PREFIX)))
        elif item.startswith(_OTHER_PREFIX):
            others.append(_parse_other(item.removeprefix(_OTHER_PREFIX)))
        else:
            raise FilterError(FilterCode.UNKNOWN_KIND)
    return Filter(
        attrs=tuple(attrs),
        themes=tuple(themes),
        others=tuple(others),
        with_duplicates=with_duplicates,
    )


def _parse_attr(rest: str) -> AttrFilter:
    # partition splits on the first "=" only, so a value that itself
    # contains "=" keeps it (docs/02 step 11).
    column, sep, value = rest.partition("=")
    if not sep or not value:
        raise FilterError(FilterCode.EMPTY_VALUE)
    return AttrFilter(column=column, value=value)


def _parse_theme(rest: str) -> str:
    if not rest:
        raise FilterError(FilterCode.EMPTY_VALUE)
    return rest


def _parse_other(rest: str) -> OtherFilter:
    question, sep, key = rest.partition(_OTHER_THEME_MARKER)
    if not sep or not question or not key:
        raise FilterError(FilterCode.MALFORMED_OTHER)
    return OtherFilter(question=question, key=key)


@dataclass(frozen=True)
class Scope:
    """The `scope` CTE and the values its placeholders name. A query over
    it follows as `scope.sql + sql.SQL("SELECT ... FROM scope ...")`, run
    with `scope.params`."""

    sql: sql.Composed
    params: dict[str, object]


# docs/04 section 6, the CTE's head. The join to question is the
# department scope below and gives the other: predicate its consultation.
_HEAD = sql.SQL(
    """WITH scope AS (
  SELECT a.id, a.respondent_id, a.value_text, a.duplicate_of_answer_id
    FROM answer a
    JOIN respondent r ON r.id = a.respondent_id
    JOIN question q ON q.id = a.question_id
   WHERE """
)
_AND = sql.SQL("\n     AND ")
_TAIL = sql.SQL("\n)\n")

_THIS_QUESTION = sql.SQL("a.question_id = {} AND NOT a.is_blank").format(
    sql.Placeholder("question_id")
)
# Answer and respondent both carry department_id, and each has to carry
# the question's, so an id guessed from another department reaches no row
# (THREAT_MODEL.md row 5; docs/02 section 10's mandatory scope).
_SAME_DEPARTMENT = sql.SQL(
    "a.department_id = q.department_id AND r.department_id = q.department_id"
)
# Answer-level and respondent-level duplicates, hidden unless the filter
# says with=duplicates (docs/02 section 7, decision 9).
_NO_DUPLICATES = (
    sql.SQL("a.duplicate_of_answer_id IS NULL"),
    sql.SQL("r.duplicate_of IS NULL"),
)
_ATTR_PROBE = sql.SQL("r.attrs @> {}::jsonb")


def _latest_signed_off(question: sql.Composable) -> sql.Composed:
    # A reopen leaves the earlier signed-off version in place beside the
    # new one (transitions.reopen_for_correction), so the tags that count
    # are the highest version_no's. No signed-off version gives NULL, and
    # a theme predicate against NULL matches nothing.
    return sql.SQL(
        """(SELECT v.id FROM theme_set_version v
                                                  WHERE v.question_id = {} AND v.status = 'signed_off'
                                                  ORDER BY v.version_no DESC LIMIT 1)"""
    ).format(question)


# theme:<key>, OR'd on this question: the keys resolve inside SQL against
# the question's latest signed-off version. Its subselect names only the
# question id parameter, so Postgres evaluates it once, not per row.
_THEMES = sql.SQL(
    """EXISTS (SELECT 1 FROM answer_theme t
                  JOIN theme th ON th.id = t.theme_id
                 WHERE t.answer_id = a.id
                   AND t.theme_set_version_id = {version}
                   AND th.key = ANY({themes}::text[]) AND t.retracted_at IS NULL)"""
).format(
    version=_latest_signed_off(sql.Placeholder("question_id")), themes=sql.Placeholder("themes")
)


def _other(number: int) -> sql.Composed:
    """other:<question>.theme=<key>, docs/04 section 6's semi-join: the same
    respondent tagged `key` on the question named by `column_ref` in this
    consultation, against that question's own latest signed-off version."""
    return sql.SQL(
        """EXISTS (SELECT 1 FROM answer a2
                   JOIN question q2 ON q2.id = a2.question_id
                   JOIN answer_theme t2 ON t2.answer_id = a2.id
                   JOIN theme th2 ON th2.id = t2.theme_id
                  WHERE a2.respondent_id = a.respondent_id
                    AND q2.consultation_id = q.consultation_id AND q2.column_ref = {question}
                    AND t2.theme_set_version_id = {version}
                    AND th2.key = {key} AND t2.retracted_at IS NULL)"""
    ).format(
        question=sql.Placeholder(f"other_question_{number}"),
        version=_latest_signed_off(sql.SQL("q2.id")),
        key=sql.Placeholder(f"other_key_{number}"),
    )


def _by_column(attrs: Iterable[AttrFilter]) -> list[list[AttrFilter]]:
    groups: dict[str, list[AttrFilter]] = {}
    for attr in attrs:
        groups.setdefault(attr.column, []).append(attr)
    return list(groups.values())


def scope(question_id: UUID, filter: Filter) -> Scope:
    """docs/04 section 6's CTE for one question under `filter`. Each
    predicate is there only when the filter carries it, in the design's
    order: the duplicate toggle, attr:, theme:, then other:."""
    params: dict[str, object] = {"question_id": question_id}
    predicates: list[sql.Composable] = [_THIS_QUESTION, _SAME_DEPARTMENT]
    if not filter.with_duplicates:
        predicates.extend(_NO_DUPLICATES)

    # One probe per value, values always arrays (docs/04 section 5). Two
    # values in one column are OR'd, two columns ANDed: section 5's
    # "two values in one group" and "two groups".
    numbers = itertools.count()
    for group in _by_column(filter.attrs):
        probes: list[sql.Composable] = []
        for attr in group:
            name = f"attr_{next(numbers)}"
            params[name] = Jsonb({attr.column: [attr.value]})
            probes.append(_ATTR_PROBE.format(sql.Placeholder(name)))
        if len(probes) == 1:
            predicates.append(probes[0])
        else:
            predicates.append(sql.SQL("({})").format(sql.SQL(" OR ").join(probes)))

    if filter.themes:
        params["themes"] = list(filter.themes)
        predicates.append(_THEMES)

    for number, other in enumerate(filter.others):
        params[f"other_question_{number}"] = other.question
        params[f"other_key_{number}"] = other.key
        predicates.append(_other(number))

    return Scope(sql=_HEAD + _AND.join(predicates) + _TAIL, params=params)
