"""What the filter grammar promises: docs/02 step 11's four forms parse to
a typed value, and anything else is refused by code, never by the value
that triggered it (CLAUDE.md rule 8: a filter value is whatever a user
typed into the address bar).

Pure module: the database tests of `query.py` (the scope CTE, the theme
table, the semi-join) are `test_query_db.py`, marked `db` there because
they run the builder against Postgres; this file imports no psycopg and
carries no `pytestmark` (`test_repo_rules.py`).
"""

from __future__ import annotations

import pytest

from consult.query import AttrFilter, Filter, FilterCode, FilterError, OtherFilter, parse_filters


def _refusal(items: list[str]) -> FilterError:
    with pytest.raises(FilterError) as raised:
        parse_filters(items)
    return raised.value


def test_the_filter_grammar_parses_three_kinds_and_refuses_the_rest() -> None:
    # attr:<column>=<value>, docs/02 step 11 and screen 4's own example.
    villages = parse_filters(["attr:d_area=Villages"])
    assert villages.attrs == (AttrFilter("d_area", "Villages"),)
    assert villages.themes == ()
    assert villages.others == ()
    assert villages.with_duplicates is False

    # The value keeps its spaces, and any "=" after the first.
    spaced = parse_filters(["attr:d_area=Outside the district"])
    assert spaced.attrs == (AttrFilter("d_area", "Outside the district"),)
    with_equals = parse_filters(["attr:d_area=Foo=Bar"])
    assert with_equals.attrs == (AttrFilter("d_area", "Foo=Bar"),)

    # theme:<key> repeated is OR'd, in the order given.
    themed = parse_filters(["theme:lighting_after_dark", "theme:noise_disruption"])
    assert themed.themes == ("lighting_after_dark", "noise_disruption")

    # other:<question>.theme=<key>, screen 4's own semi-join example.
    other = parse_filters(["other:o_safety.theme=lighting_after_dark"])
    assert other.others == (OtherFilter("o_safety", "lighting_after_dark"),)

    # with=duplicates flips the toggle; absent, it stays off.
    assert parse_filters(["with=duplicates"]).with_duplicates is True
    assert parse_filters(["attr:d_area=Villages"]).with_duplicates is False

    # The empty list parses to a filter with nothing set.
    assert parse_filters([]) == Filter()

    # An unknown kind is refused by code.
    unknown = _refusal(["foo:bar"])
    assert unknown.code is FilterCode.UNKNOWN_KIND
    assert str(unknown) == FilterCode.UNKNOWN_KIND.value

    # An empty value is refused by code, for both attr: and theme:.
    for item in ("attr:d_area=", "theme:"):
        empty = _refusal([item])
        assert empty.code is FilterCode.EMPTY_VALUE
        assert str(empty) == FilterCode.EMPTY_VALUE.value

    # A malformed other: is refused by code, whichever part is missing.
    for item in ("other:o_safety=key", "other:.theme=key", "other:o_safety.theme="):
        malformed = _refusal([item])
        assert malformed.code is FilterCode.MALFORMED_OTHER
        assert str(malformed) == FilterCode.MALFORMED_OTHER.value

    # The value that triggered a refusal never reaches the message: it
    # could be anything a user typed (CLAUDE.md rule 8).
    leaked = _refusal(["other:o_safety=a-secret-value"])
    assert "a-secret-value" not in str(leaked)


def test_a_nul_or_a_lone_surrogate_in_a_filter_is_refused_by_code() -> None:
    # Postgres text can't hold a NUL, and a lone surrogate, which is what
    # invalid UTF-8 in argv decodes to, has no UTF-8 encoding. The server
    # refuses either inside jsonb (22P05, 22P02) with a CONTEXT line that
    # reprints the value, so the grammar refuses both first, by code, in
    # every slot it parses (CLAUDE.md rule 8).
    for bad in ("\x00", "\udcff"):
        for item in (
            f"attr:d_{bad}area=Villages",
            f"attr:d_area=Vill{bad}ages",
            f"theme:PARK{bad}ING",
            f"other:o_{bad}safety.theme=LIGHTING",
            f"other:o_safety.theme=LIGHT{bad}ING",
        ):
            refused = _refusal([item])
            assert refused.code is FilterCode.UNREADABLE_VALUE
            assert str(refused) == FilterCode.UNREADABLE_VALUE.value
            assert bad not in str(refused)


def test_an_empty_attr_column_is_refused() -> None:
    # attr:=Villages names no column: its probe would be {"": ["Villages"]},
    # which matches nobody and says so nowhere. It's refused like an empty
    # value.
    refused = _refusal(["attr:=Villages"])
    assert refused.code is FilterCode.EMPTY_VALUE
    assert str(refused) == FilterCode.EMPTY_VALUE.value
