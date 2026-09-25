"""The configure step: the definition, the report and the resolutions
become the rows the service trusts (docs/02, step 3).

The workbook pre-fills the screen; it isn't the configuration. Options are
rows, so a label can contain a comma (docs/00). Each warning the
validator raised has a resolution recorded here, as a value policy on the
question (the N/A decision, the mapping for an unknown value) or as a
merge of two options, and each unmatched header a role. Saving twice
upserts on (consultation_id, column_ref) rather than duplicating
(docs/04, section 3). Nothing here commits.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from consult import transitions
from consult.definition import ClosedQuestion, Definition, ResponseType
from consult.validate import Report, Resolution, WarningKind

RESPONSE_TYPES = {
    ResponseType.SINGLE_SELECT: "single_select",
    ResponseType.LIKERT_5: "likert_5",
    ResponseType.MULTI_SELECT: "multi_select",
}


@dataclass(frozen=True)
class Resolutions:
    """What the reviewer decided at configure time, one entry per warning.

    unknown_values maps (column, value) to the option it stands for, added
    to the vocabulary if it's new, or to None for "treat as not answered".
    not_applicable says per column whether N/A stays a value. merges lists
    the adjacent option pairs that were one option before the workbook
    split them. roles gives each unmatched header its role.
    """

    unknown_values: Mapping[tuple[str, str], str | None]
    not_applicable: Mapping[str, bool]
    merges: Mapping[str, tuple[tuple[str, str], ...]]
    roles: Mapping[str, Resolution]
    duplicate_ids: Resolution | None


@dataclass(frozen=True)
class Configured:
    questions: dict[str, UUID]


def defaults(report: Report) -> Resolutions:
    """Every warning's default: unknown values not answered, N/A kept, the
    never-apart pairs merged, roles as suggested, duplicates kept-first."""
    unknown: dict[tuple[str, str], str | None] = {}
    not_applicable: dict[str, bool] = {}
    merges: dict[str, tuple[tuple[str, str], ...]] = {}
    roles: dict[str, Resolution] = {}
    duplicate_ids: Resolution | None = None
    for warning in report.warnings:
        if warning.kind is WarningKind.UNKNOWN_VALUE and warning.value is not None:
            unknown[warning.column_ref, warning.value] = None
        elif warning.kind is WarningKind.NOT_APPLICABLE:
            not_applicable[warning.column_ref] = warning.default is Resolution.KEEP_AS_VALUE
        elif warning.kind is WarningKind.OPTIONS_NEVER_APART and warning.value is not None:
            first, _, second = warning.value.partition(", ")
            merges[warning.column_ref] = (*merges.get(warning.column_ref, ()), (first, second))
        elif warning.kind is WarningKind.UNMATCHED_HEADER:
            roles[warning.column_ref] = warning.default
        elif warning.kind is WarningKind.DUPLICATE_RESPONDENT_ID:
            duplicate_ids = warning.default
    return Resolutions(unknown, not_applicable, merges, roles, duplicate_ids)


def merged_options(question: ClosedQuestion, resolutions: Resolutions) -> list[str]:
    """The options as configured: adjacent pairs the workbook split are one
    option again, and a mapping to a new label adds it."""
    options = list(question.options)
    for first, second in resolutions.merges.get(question.column_ref, ()):
        for index in range(len(options) - 1):
            if (options[index], options[index + 1]) == (first, second):
                options[index : index + 2] = [f"{first}, {second}"]
                break
    for (column_ref, _value), target in resolutions.unknown_values.items():
        if column_ref == question.column_ref and target is not None and target not in options:
            options.append(target)
    return options


def _policy(column_ref: str, resolutions: Resolutions) -> dict[str, object]:
    policy: dict[str, object] = {
        "not_applicable": (
            "keep_as_value"
            if resolutions.not_applicable.get(column_ref, True)
            else "treat_as_not_answered"
        )
    }
    mappings = {
        value: target
        for (ref, value), target in resolutions.unknown_values.items()
        if ref == column_ref
    }
    if mappings:
        policy["unknown_values"] = mappings
    return policy


def _upsert_question(
    conn: psycopg.Connection[DictRow],
    consultation_id: UUID,
    *,
    column_ref: str,
    text: str,
    kind: str,
    ordinal: int,
    response_type: str | None = None,
    status: str | None = None,
    policy: Mapping[str, object] | None = None,
) -> UUID:
    row = conn.execute(
        """
        INSERT INTO question (department_id, consultation_id, column_ref, question_text, kind,
                              response_type, ordinal, status, value_policy)
        SELECT department_id, id, %(column_ref)s, %(text)s, %(kind)s, %(response_type)s,
               %(ordinal)s, %(status)s, %(policy)s
          FROM consultation WHERE id = %(consultation_id)s
        ON CONFLICT (consultation_id, column_ref) DO UPDATE
           SET question_text = EXCLUDED.question_text, kind = EXCLUDED.kind,
               response_type = EXCLUDED.response_type, ordinal = EXCLUDED.ordinal,
               value_policy = EXCLUDED.value_policy
        RETURNING id
        """,
        {
            "consultation_id": consultation_id,
            "column_ref": column_ref,
            "text": text,
            "kind": kind,
            "response_type": response_type,
            "ordinal": ordinal,
            "status": status,
            "policy": Jsonb(dict(policy or {})),
        },
    ).fetchone()
    if row is None:
        raise transitions.TransitionError(f"consultation {consultation_id}: question not written")
    return UUID(str(row["id"]))


def configure(
    conn: psycopg.Connection[DictRow],
    consultation_id: UUID,
    definition: Definition,
    report: Report,
    resolutions: Resolutions,
) -> Configured:
    ordinals = {column.column_ref: index for index, column in enumerate(report.columns, start=1)}
    questions: dict[str, UUID] = {}
    ignored: list[str] = []
    respondent_id: str | None = None
    for header, role in resolutions.roles.items():
        if role is Resolution.ROLE_RESPONDENT_ID and respondent_id is None:
            respondent_id = header
        elif role is Resolution.ROLE_IGNORE or role is Resolution.ROLE_RESPONDENT_ID:
            ignored.append(header)
        elif role is Resolution.ROLE_IDENTITY:
            questions[header] = _upsert_question(
                conn,
                consultation_id,
                column_ref=header,
                text=header,
                kind="identity",
                ordinal=ordinals[header],
            )
    for demographic in definition.demographic:
        questions[demographic.column_ref] = _upsert_question(
            conn,
            consultation_id,
            column_ref=demographic.column_ref,
            text=demographic.text,
            kind="demographic",
            ordinal=ordinals[demographic.column_ref],
            policy=_policy(demographic.column_ref, resolutions),
        )
    for closed in definition.closed:
        question_id = _upsert_question(
            conn,
            consultation_id,
            column_ref=closed.column_ref,
            text=closed.text,
            kind="closed",
            response_type=RESPONSE_TYPES[closed.response_type],
            ordinal=ordinals[closed.column_ref],
            policy=_policy(closed.column_ref, resolutions),
        )
        questions[closed.column_ref] = question_id
        for ordinal, label in enumerate(merged_options(closed, resolutions), start=1):
            conn.execute(
                """
                INSERT INTO question_option (department_id, question_id, label, ordinal)
                SELECT department_id, id, %s, %s FROM question WHERE id = %s
                ON CONFLICT (question_id, label) DO UPDATE SET ordinal = EXCLUDED.ordinal
                """,
                (label, ordinal, question_id),
            )
    for opened in definition.open:
        question_id = _upsert_question(
            conn,
            consultation_id,
            column_ref=opened.column_ref,
            text=opened.text,
            kind="open",
            ordinal=ordinals[opened.column_ref],
            status="configured",
        )
        questions[opened.column_ref] = question_id
        related = questions.get(opened.related_closed_column or "")
        conn.execute(
            "UPDATE question SET related_closed_question_id = %s WHERE id = %s",
            (related, question_id),
        )
    transitions.record_column_roles(
        conn,
        consultation_id,
        {
            "respondent_id": respondent_id,
            "ignore": ignored,
            "duplicate_ids": resolutions.duplicate_ids.value if resolutions.duplicate_ids else None,
        },
    )
    return Configured(questions)
