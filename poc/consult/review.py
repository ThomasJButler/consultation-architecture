"""The reviewer's edits to a candidate theme set (docs/02, step 8; ADR-003).

Rename, merge, split, add and remove, each a guarded UPDATE on the
version's `edit_version` (docs/04): the reviewer says which version they
were looking at, and if another reviewer got there first the edit is
refused with a conflict rather than applied over theirs. Keys never
change, because the key is the enum the model returns (docs/02, step 9)
and a preview or a mapping already running is labelling against it.
Nothing here deletes a row: a removed or merged theme moves to the
longlist with its lineage, and a split leaves the original there too. A
version that isn't a candidate takes no edits. Every edit checks its
keys before it takes the guard, so a refused edit leaves the counter
where it was and the reviewer's next attempt still carries the right
number. Nothing here commits.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from consult.replies import KEY_PATTERN
from consult.transitions import RESERVED_KEYS

_KEY = re.compile(KEY_PATTERN)


class ReviewError(Exception):
    """An edit that can't be applied as given: a key nobody has, or one
    already taken, or one that isn't a key."""


class EditConflictError(ReviewError):
    """The version isn't a candidate at the edit_version the reviewer was
    looking at: someone else edited, or signed it off (docs/02, screen 3)."""


@dataclass(frozen=True)
class Edited:
    version_id: UUID
    edit_version: int


def _guard(conn: psycopg.Connection[DictRow], version_id: UUID, expected_version: int) -> Edited:
    row = conn.execute(
        """
        UPDATE theme_set_version SET edit_version = edit_version + 1
         WHERE id = %s AND status = 'candidate' AND edit_version = %s
        RETURNING edit_version
        """,
        (version_id, expected_version),
    ).fetchone()
    if row is None:
        raise EditConflictError(
            f"version {version_id} is not a candidate at edit {expected_version}"
        )
    return Edited(version_id, int(row["edit_version"]))


def _theme_id(conn: psycopg.Connection[DictRow], version_id: UUID, key: str) -> UUID:
    row = conn.execute(
        "SELECT id FROM theme WHERE theme_set_version_id = %s AND key = %s", (version_id, key)
    ).fetchone()
    if row is None:
        raise ReviewError(f"version {version_id} has no theme with that key")
    theme_id: UUID = row["id"]
    return theme_id


def _check_new(conn: psycopg.Connection[DictRow], version_id: UUID, key: str, label: str) -> None:
    if not _KEY.fullmatch(key):
        raise ReviewError("a key is upper case letters, digits and underscores, 2 to 40 long")
    if key in RESERVED_KEYS:
        raise ReviewError("that key is one sign-off adds itself")
    if not label:
        raise ReviewError("a label is required")
    taken = conn.execute(
        "SELECT 1 FROM theme WHERE theme_set_version_id = %s AND key = %s", (version_id, key)
    ).fetchone()
    if taken is not None:
        raise ReviewError(f"version {version_id} already has a theme with that key")


def _insert(
    conn: psycopg.Connection[DictRow],
    version_id: UUID,
    key: str,
    label: str,
    description: str,
    lineage: UUID | None,
) -> UUID:
    row = conn.execute(
        """
        INSERT INTO theme (department_id, theme_set_version_id, key, label, description,
                           is_longlist, lineage_theme_id, preview_count)
        SELECT department_id, id, %(key)s, %(label)s, %(description)s, false, %(lineage)s, 0
          FROM theme_set_version WHERE id = %(version)s
        ON CONFLICT (theme_set_version_id, key) DO NOTHING
        RETURNING id
        """,
        {
            "key": key,
            "label": label,
            "description": description,
            "lineage": lineage,
            "version": version_id,
        },
    ).fetchone()
    if row is None:
        raise ReviewError(f"version {version_id} already has a theme with that key")
    theme_id: UUID = row["id"]
    return theme_id


def rename(
    conn: psycopg.Connection[DictRow],
    version_id: UUID,
    key: str,
    *,
    label: str,
    description: str | None = None,
    expected_version: int,
) -> Edited:
    """A new label (and description) under the same key."""
    if not label:
        raise ReviewError("a label is required")
    theme_id = _theme_id(conn, version_id, key)
    edited = _guard(conn, version_id, expected_version)
    conn.execute(
        "UPDATE theme SET label = %s, description = coalesce(%s, description) WHERE id = %s",
        (label, description, theme_id),
    )
    return edited


def add(
    conn: psycopg.Connection[DictRow],
    version_id: UUID,
    *,
    key: str,
    label: str,
    description: str,
    expected_version: int,
) -> Edited:
    """A theme the model didn't propose, on the shortlist with no count yet."""
    _check_new(conn, version_id, key, label)
    edited = _guard(conn, version_id, expected_version)
    _insert(conn, version_id, key, label, description, None)
    return edited


def remove(
    conn: psycopg.Connection[DictRow], version_id: UUID, key: str, *, expected_version: int
) -> Edited:
    """Off the shortlist and onto the longlist; the row stays."""
    theme_id = _theme_id(conn, version_id, key)
    edited = _guard(conn, version_id, expected_version)
    conn.execute("UPDATE theme SET is_longlist = true WHERE id = %s", (theme_id,))
    return edited


def merge(
    conn: psycopg.Connection[DictRow],
    version_id: UUID,
    keys: Sequence[str],
    *,
    into: str,
    expected_version: int,
) -> Edited:
    """Fold themes into one that stays: the folded ones go to the longlist
    pointing at it, and their preview counts are added to its."""
    survivor = _theme_id(conn, version_id, into)
    folded_ids = []
    for key in keys:
        if key == into:
            raise ReviewError("a theme can't merge into itself")
        folded_ids.append(_theme_id(conn, version_id, key))
    edited = _guard(conn, version_id, expected_version)
    for folded in folded_ids:
        conn.execute(
            """
            UPDATE theme s SET preview_count = coalesce(s.preview_count, 0) + coalesce(f.preview_count, 0)
              FROM theme f WHERE s.id = %s AND f.id = %s
            """,
            (survivor, folded),
        )
        conn.execute(
            "UPDATE theme SET is_longlist = true, lineage_theme_id = %s WHERE id = %s",
            (survivor, folded),
        )
    return edited


def split(
    conn: psycopg.Connection[DictRow],
    version_id: UUID,
    key: str,
    *,
    into: Sequence[tuple[str, str, str]],
    expected_version: int,
) -> Edited:
    """One theme into several: the new ones on the shortlist with lineage
    back to it, the original onto the longlist."""
    if len(into) < 2:
        raise ReviewError("a split needs at least two themes")
    original = _theme_id(conn, version_id, key)
    for new_key, label, _description in into:
        _check_new(conn, version_id, new_key, label)
    edited = _guard(conn, version_id, expected_version)
    for new_key, label, description in into:
        _insert(conn, version_id, new_key, label, description, original)
    conn.execute("UPDATE theme SET is_longlist = true WHERE id = %s", (original,))
    return edited
