"""What the reviewer's edits promise (docs/02, step 8; ADR-003; docs/04,
`theme_set_version.edit_version`).

Rename, merge, split, add and remove on a candidate version, each guarded
by the version's edit counter so two reviewers on one question get a
conflict rather than a silent overwrite. Keys stay stable through a
rename; nothing is deleted, a removed or merged theme moves to the
longlist with its lineage; a signed-off version takes no edits.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import DictRow

from consult.review import EditConflictError, Edited, ReviewError, add, merge, remove, rename, split
from tests.rows import (
    make_consultation,
    make_department,
    make_open_question,
    make_theme,
    make_theme_set_version,
)

pytestmark = pytest.mark.db


def themes_of(db: psycopg.Connection[DictRow], version_id: object) -> dict[str, DictRow]:
    rows = db.execute(
        """
        SELECT id, key, label, description, is_longlist, lineage_theme_id, preview_count
          FROM theme WHERE theme_set_version_id = %s ORDER BY key
        """,
        (version_id,),
    ).fetchall()
    return {str(row["key"]): row for row in rows}


def edit_version_of(db: psycopg.Connection[DictRow], version_id: object) -> int:
    row = db.execute(
        "SELECT edit_version FROM theme_set_version WHERE id = %s", (version_id,)
    ).fetchone()
    assert row is not None
    return int(row["edit_version"])


def test_edits_are_guarded_by_the_version(db: psycopg.Connection[DictRow]) -> None:
    consultation_id = make_consultation(db, make_department(db), status="awaiting_review")
    question_id = make_open_question(db, consultation_id, status="themes_ready")
    version_id = make_theme_set_version(db, question_id)
    for key, count in (("PARKING", 9), ("SAFETY", 7), ("ACCESS", 5)):
        theme_id = make_theme(db, version_id, key)
        db.execute("UPDATE theme SET preview_count = %s WHERE id = %s", (count, theme_id))
    assert edit_version_of(db, version_id) == 0

    # Rename: the label moves, the key doesn't, the counter steps once.
    assert rename(
        db, version_id, "PARKING", label="Parking on Mill Lane", expected_version=0
    ) == Edited(version_id, 1)
    assert themes_of(db, version_id)["PARKING"]["label"] == "Parking on Mill Lane"
    # A stale expected version is refused and changes nothing.
    with pytest.raises(EditConflictError):
        rename(db, version_id, "PARKING", label="Something else", expected_version=0)
    assert themes_of(db, version_id)["PARKING"]["label"] == "Parking on Mill Lane"
    assert edit_version_of(db, version_id) == 1

    # Add: a new shortlist theme with a well-formed key.
    assert add(
        db,
        version_id,
        key="COST",
        label="Cost",
        description="Cost to the council",
        expected_version=1,
    ) == Edited(version_id, 2)
    added = themes_of(db, version_id)["COST"]
    assert (added["is_longlist"], added["lineage_theme_id"], added["preview_count"]) == (
        False,
        None,
        0,
    )
    with pytest.raises(ReviewError):
        add(db, version_id, key="COST", label="Twice", description="", expected_version=2)
    with pytest.raises(ReviewError):
        add(db, version_id, key="not a key", label="Bad", description="", expected_version=2)
    assert edit_version_of(db, version_id) == 2

    # Merge: ACCESS folds into SAFETY, kept on the longlist with lineage, its
    # preview count carried over.
    assert merge(db, version_id, ["ACCESS"], into="SAFETY", expected_version=2) == Edited(
        version_id, 3
    )
    after_merge = themes_of(db, version_id)
    assert after_merge["ACCESS"]["is_longlist"] is True
    assert after_merge["ACCESS"]["lineage_theme_id"] == after_merge["SAFETY"]["id"]
    assert after_merge["SAFETY"]["preview_count"] == 12

    # Split: COST becomes two shortlist themes with lineage back to it, and
    # itself goes to the longlist.
    assert split(
        db,
        version_id,
        "COST",
        into=[("COST_TOLL", "Tolls", "Tolls on the bridge"), ("COST_TAX", "Council tax", "")],
        expected_version=3,
    ) == Edited(version_id, 4)
    after_split = themes_of(db, version_id)
    assert after_split["COST"]["is_longlist"] is True
    assert {after_split[k]["lineage_theme_id"] for k in ("COST_TOLL", "COST_TAX")} == {
        after_split["COST"]["id"]
    }
    assert {after_split[k]["is_longlist"] for k in ("COST_TOLL", "COST_TAX")} == {False}

    # Remove: to the longlist, not gone. Nothing here deletes.
    assert remove(db, version_id, "PARKING", expected_version=4) == Edited(version_id, 5)
    final = themes_of(db, version_id)
    assert final["PARKING"]["is_longlist"] is True
    assert len(final) == 6
    assert [k for k, t in final.items() if not t["is_longlist"]] == [
        "COST_TAX",
        "COST_TOLL",
        "SAFETY",
    ]

    # An unknown key, and a version that isn't a candidate.
    with pytest.raises(ReviewError):
        rename(db, version_id, "NOPE", label="x", expected_version=5)
    db.execute("UPDATE theme_set_version SET status = 'signed_off' WHERE id = %s", (version_id,))
    with pytest.raises(EditConflictError):
        rename(db, version_id, "SAFETY", label="x", expected_version=5)
