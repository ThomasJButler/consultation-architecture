"""Connections and the schema. Every SQL statement the design rests on lives
here or in a sibling module, parameterised and readable (CLAUDE.md, rule 10).
"""

from __future__ import annotations

from importlib import resources

import psycopg
from psycopg.rows import DictRow, dict_row

from consult.config import Settings


def connect(settings: Settings, *, autocommit: bool = False) -> psycopg.Connection[DictRow]:
    """One connection with dict rows, so a query result is typed by its keys.

    Modules that want a dataclass per row open a cursor with `class_row` on
    this connection; that is the typed path CLAUDE.md rule 7 asks for.
    """
    return psycopg.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        autocommit=autocommit,
        row_factory=dict_row,
        application_name="consult",
    )


def schema_sql() -> str:
    return resources.files("consult").joinpath("schema.sql").read_text(encoding="utf-8")


def init(conn: psycopg.Connection[DictRow]) -> None:
    """Apply schema.sql. Idempotent: every statement in it is IF NOT EXISTS."""
    conn.execute(schema_sql())
    conn.commit()
