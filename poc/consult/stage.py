"""The stage step: the file into a logged table in the staging schema.

docs/02 step 2: the worker COPYs the file into a staging table, and the
validator runs over it before any spend. docs/04 section 2 says where it
goes: one logged table per upload, `staging."<consultation id>"`, written
and later dropped by the ingest role, which the pipeline role can't read
because identity columns sit here until ingest moves them to the vault.
Logged, because it lives across the human configure step and Postgres
truncates an unlogged table after a crash (docs/01, section 6). The
columns are this module's choice: every one text, plus row_no for the
file's row number, which ingest carries into respondent.source_row_no.
Nothing here commits.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import DictRow

from consult import transitions
from consult.inputs import DEFAULT_CAPS, Caps
from consult.responses import Responses
from consult.store import as_role
from consult.validate import MAX_HEADER_BYTES

INGEST_ROLE = "consult_ingest"


class StageError(Exception):
    """The file's header can't be a table: a blank or repeated name, or one
    Postgres would truncate. The validator reports these as errors first;
    this is the backstop. The message carries counts, never a cell."""


@dataclass(frozen=True)
class Staged:
    rows: int
    sha256: bytes


def staging_table(consultation_id: UUID) -> sql.Composed:
    return sql.SQL(".").join((sql.Identifier("staging"), sql.Identifier(str(consultation_id))))


def _sha256(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.digest()


def stage(
    conn: psycopg.Connection[DictRow], consultation_id: UUID, path: Path, caps: Caps = DEFAULT_CAPS
) -> Staged:
    responses = Responses(path, caps)
    header = responses.header
    if "" in header or len(set(header)) != len(header):
        raise StageError(f"{len(header)} headers, not all distinct and named")
    too_long = sum(len(name.encode()) > MAX_HEADER_BYTES for name in header)
    if too_long:
        raise StageError(f"{too_long} headers over {MAX_HEADER_BYTES} bytes")
    transitions.start_staging(conn, consultation_id)
    table = staging_table(consultation_id)
    columns = [sql.Identifier(name) for name in header]
    rows = 0
    with as_role(conn, INGEST_ROLE):
        conn.execute(
            sql.SQL("CREATE TABLE {} (row_no integer PRIMARY KEY, {})").format(
                table, sql.SQL(", ").join(sql.SQL("{} text").format(column) for column in columns)
            )
        )
        with conn.cursor().copy(
            sql.SQL("COPY {} (row_no, {}) FROM STDIN").format(table, sql.SQL(", ").join(columns))
        ) as copy:
            for row in responses.rows():
                copy.write_row([row.no, *(row.cells[name] for name in header)])
                rows += 1
    transitions.mark_staged(conn, consultation_id, upload_sha256=_sha256(path), row_count=rows)
    return Staged(rows, _sha256(path))
