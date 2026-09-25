"""What the configure step promises (docs/02, step 3).

The workbook pre-fills; the app's configuration is what the service
trusts. Here that means: one question row per column the workbook
describes, options as rows (so a label can contain a comma), the
respondent id and ignored columns recorded as roles, the N/A decision and
the unknown-value mappings recorded as a policy per question, and saving
twice upserting rather than duplicating (docs/04, section 3).
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from psycopg.rows import DictRow

from consult.configure import Resolutions, configure, defaults
from consult.definition import read_definition
from consult.responses import Responses
from consult.validate import validate
from tests.rows import make_consultation, make_department

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def questions_of(db: psycopg.Connection[DictRow], consultation_id: object) -> list[DictRow]:
    return db.execute(
        """
        SELECT column_ref, kind, response_type, ordinal, status, related_closed_question_id,
               value_policy, id
          FROM question WHERE consultation_id = %s ORDER BY ordinal
        """,
        (consultation_id,),
    ).fetchall()


def options_of(db: psycopg.Connection[DictRow], question_id: object) -> list[str]:
    rows = db.execute(
        "SELECT label FROM question_option WHERE question_id = %s ORDER BY ordinal", (question_id,)
    ).fetchall()
    return [row["label"] for row in rows]


def test_configure_writes_questions_options_roles_and_policies(
    db: psycopg.Connection[DictRow],
) -> None:
    consultation_id = make_consultation(db, make_department(db), status="staged")
    definition = read_definition(FIXTURES / "definition.xlsx")
    report = validate(definition, Responses(FIXTURES / "responses.csv"))
    chosen = defaults(report)
    # The reviewer maps the fourteen "Unsure" rows to an option that exists.
    resolutions = Resolutions(
        unknown_values={**chosen.unknown_values, ("c_route", "Unsure"): "Not sure"},
        not_applicable=chosen.not_applicable,
        merges=chosen.merges,
        roles=chosen.roles,
        duplicate_ids=chosen.duplicate_ids,
    )

    configured = configure(db, consultation_id, definition, report, resolutions)

    rows = questions_of(db, consultation_id)
    # Header order, minus the id and ignored columns; identity gets a row
    # so the export can name it (docs/04, section 1).
    assert [(r["column_ref"], r["kind"], r["ordinal"]) for r in rows] == [
        ("email", "identity", 2),
        ("d_area", "demographic", 3),
        ("d_commute", "demographic", 4),
        ("d_age", "demographic", 5),
        ("c_route", "closed", 6),
        ("c_modes", "closed", 7),
        ("c_safety", "closed", 8),
        ("o_reason", "open", 9),
        ("o_safety", "open", 10),
    ]
    by_ref = {r["column_ref"]: r for r in rows}
    assert configured.questions == {ref: r["id"] for ref, r in by_ref.items()}
    assert [by_ref[ref]["response_type"] for ref in ("c_route", "c_modes", "c_safety")] == [
        "single_select",
        "multi_select",
        "likert_5",
    ]
    assert [by_ref[ref]["status"] for ref in ("o_reason", "o_safety")] == [
        "configured",
        "configured",
    ]
    assert by_ref["o_reason"]["related_closed_question_id"] == by_ref["c_route"]["id"]
    assert by_ref["o_safety"]["related_closed_question_id"] is None
    assert by_ref["d_area"]["status"] is None

    # Options as rows, the comma option merged back into one.
    assert options_of(db, by_ref["c_route"]["id"]) == ["Support", "Oppose", "Not sure"]
    assert options_of(db, by_ref["c_modes"]["id"]) == [
        "Cycle",
        "Walk",
        "Run",
        "Wheelchair, mobility scooter or similar",
        "Push a pram",
    ]

    assert by_ref["d_commute"]["value_policy"] == {"not_applicable": "keep_as_value"}
    assert by_ref["c_route"]["value_policy"] == {
        "not_applicable": "keep_as_value",
        "unknown_values": {"Unsure": "Not sure"},
    }
    roles = db.execute(
        "SELECT column_roles FROM consultation WHERE id = %s", (consultation_id,)
    ).fetchone()
    assert roles == {
        "column_roles": {
            "respondent_id": "respondent_ref",
            "ignore": ["notes_internal"],
            "duplicate_ids": None,
        }
    }

    # Saving twice upserts: same rows, same options, same ids.
    again = configure(db, consultation_id, definition, report, resolutions)
    assert again == configured
    assert len(questions_of(db, consultation_id)) == 9
    assert options_of(db, by_ref["c_modes"]["id"]) == options_of(db, again.questions["c_modes"])
