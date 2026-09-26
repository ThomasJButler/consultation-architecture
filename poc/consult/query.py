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
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

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
