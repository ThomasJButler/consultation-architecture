"""Connections and the schema. Every SQL statement the design rests on lives
here or in a sibling module, parameterised and readable (CLAUDE.md, rule 10).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import DictRow, dict_row

from consult.config import Settings

# The fourteen tables schema.sql creates, in dependency order, so a reset can
# drop them and the test harness can truncate them (docs/04, section 8).
TABLES: tuple[str, ...] = (
    "department",
    "consultation",
    "question",
    "question_option",
    "respondent",
    "vault.respondent_identity",
    "answer",
    "theme_set_version",
    "theme",
    "theme_example",
    "job",
    "job_batch",
    "answer_theme",
    "notification_outbox",
)

SCHEMAS: tuple[str, ...] = ("vault", "staging")


def qualified(name: str) -> sql.Composable:
    """`vault.respondent_identity` as two identifiers, never as a format string."""
    schema, _, table = name.rpartition(".")
    if schema:
        return sql.SQL(".").join((sql.Identifier(schema), sql.Identifier(table)))
    return sql.Identifier(table)


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


def reset(conn: psycopg.Connection[DictRow]) -> None:
    """Drop everything schema.sql creates, then apply it again.

    Roles are cluster-wide and left alone: a role another database's grants
    hang off isn't this database's to drop, and the guarded DO block in
    schema.sql finds them already there.
    """
    for table in reversed(TABLES):
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(qualified(table)))
    for schema in SCHEMAS:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    conn.commit()
    init(conn)


@contextmanager
def as_role(conn: psycopg.Connection[DictRow], role: str) -> Iterator[None]:
    """Run the block as one of the four NOLOGIN roles (docs/06, section 2.4).

    The connection is the login user's; SET ROLE picks the grant set for
    the stage and ingest writes, so the tests prove the grants are enough
    and a missing one shows as a permission error rather than nothing.
    RESET ROLE is skipped when the transaction has already failed, since
    it would fail too and hide the real error; the rollback ends the
    transaction and the role with it.
    """
    conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
    try:
        yield
    finally:
        if conn.info.transaction_status != TransactionStatus.INERROR:
            conn.execute("RESET ROLE")
