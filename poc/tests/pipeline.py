"""The fixtures taken to the point ingest starts from: created, staged and
configured with every warning's default, on one connection."""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from consult.configure import Configured, Resolutions, configure, defaults
from consult.definition import Definition, read_definition
from consult.responses import Responses
from consult.stage import stage
from consult.validate import Report, validate
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
