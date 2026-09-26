"""The fixtures taken to the point ingest starts from: created, staged and
configured with every warning's default, on one connection."""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from consult.configure import Configured, Resolutions, configure, defaults
from consult.definition import Definition, read_definition
from consult.ingest import ingest
from consult.jobs import claim
from consult.responses import Responses
from consult.stage import stage
from consult.themes import run_find_themes
from consult.transitions import sign_off
from consult.validate import Report, validate
from tests.fakes import RecordingLLM
from tests.rows import make_consultation, make_department

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RESPONSES = FIXTURES / "responses.csv"
DEFINITION = FIXTURES / "definition.xlsx"
NOT_ANSWERED = {"", "-"}


@dataclass(frozen=True)
class Staged:
    consultation_id: UUID
    definition: Definition
    report: Report
    resolutions: Resolutions
    configured: Configured


def fixture_rows() -> list[dict[str, str]]:
    with RESPONSES.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def staged_fixture(
    db: psycopg.Connection[DictRow],
    resolutions: Resolutions | None = None,
    *,
    path: Path = RESPONSES,
    resolve: Callable[[Resolutions], Resolutions] | None = None,
) -> Staged:
    """`resolutions` replaces the defaults outright; `resolve` edits them,
    for a test that wants every default but one."""
    consultation_id = make_consultation(db, make_department(db))
    stage(db, consultation_id, path)
    definition = read_definition(DEFINITION)
    report = validate(definition, Responses(path))
    chosen = resolutions or defaults(report)
    if resolve is not None:
        chosen = resolve(chosen)
    configured = configure(db, consultation_id, definition, report, chosen)
    return Staged(consultation_id, definition, report, chosen, configured)


@dataclass(frozen=True)
class SignedOff:
    """The ids a mapping test drives `mapping.py`'s stages from by hand."""

    consultation_id: UUID
    question_id: UUID
    version_id: UUID
    job_id: UUID


def signed_off_questions(
    db: psycopg.Connection[DictRow],
    column_refs: tuple[str, ...],
    *,
    model_alias: str = "fake-model",
    seed: int = 7,
) -> dict[str, SignedOff]:
    """The fixtures ingested, then each named question's find_themes job
    run to themes_ready and signed off, and its map_themes job moved to
    queued with the alias and seed dispatch would otherwise stamp (dispatch
    is PR-08's own test; setting the two columns by hand keeps a mapping
    test about mapping). Name every open question and fan-in 1 has run:
    the consultation is awaiting_review before any map job starts."""
    staged = staged_fixture(db)
    ingest(db, staged.consultation_id)
    signed_off: dict[str, SignedOff] = {}
    for column_ref in column_refs:
        question_id = staged.configured.questions[column_ref]
        find_job = db.execute(
            """
            UPDATE job SET status = 'queued', model_alias = %s, params = %s
             WHERE question_id = %s AND kind = 'find_themes'
            RETURNING id
            """,
            (model_alias, Jsonb({"seed": seed}), question_id),
        ).fetchone()
        assert find_job is not None
        lease = claim(db, find_job["id"], "worker-1")
        assert lease is not None
        run_find_themes(db, RecordingLLM(), lease)
        signed = sign_off(db, question_id, uuid4())
        assert signed is not None
        db.execute(
            "UPDATE job SET status = 'queued', model_alias = %s, params = %s WHERE id = %s",
            (model_alias, Jsonb({"seed": seed}), signed.job_id),
        )
        signed_off[column_ref] = SignedOff(
            staged.consultation_id, question_id, signed.version_id, signed.job_id
        )
    return signed_off


def signed_off_fixture(
    db: psycopg.Connection[DictRow],
    column_ref: str = "o_reason",
    *,
    model_alias: str = "fake-model",
    seed: int = 7,
) -> SignedOff:
    """One question signed off, for the tests that map one."""
    return signed_off_questions(db, (column_ref,), model_alias=model_alias, seed=seed)[column_ref]
