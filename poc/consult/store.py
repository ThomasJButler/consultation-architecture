"""Connections and the schema. Every SQL statement the design rests on lives
here or in a sibling module, parameterised and readable (CLAUDE.md, rule 10).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from importlib import resources
from uuid import UUID

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

# The role a worker runs as: SELECT, INSERT and UPDATE on the public tables
# and no grant on the vault or the staging schema (docs/06, section 2.4 as
# corrected). The ingest role is named next to the code that uses it.
PIPELINE_ROLE = "consult_pipeline"
# The role export.py reads as: SELECT on the public tables, and SELECT on
# the vault besides, the one grant the pipeline role is refused (docs/06,
# section 2.4 as corrected; section 4, the export role's own view of the
# identity columns).
EXPORT_ROLE = "consult_export"

# Half the ten-minute lease (jobs.STALE_AFTER; docs/02, step 5). A worker
# paused inside a transaction holds its job row, and the pick and the
# re-send both skip a held row, so the lease can be taken over, as ADR-002
# means it to be, only once the server ends that transaction. No worker or
# reconciler transaction spans a model call or a sleep (worker.py's module
# docstring), so a gap between two of their statements is milliseconds.
IDLE_IN_TRANSACTION_TIMEOUT = timedelta(minutes=5)


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


def bound_idle_transactions(
    conn: psycopg.Connection[DictRow], timeout: timedelta = IDLE_IN_TRANSACTION_TIMEOUT
) -> None:
    """Have the server end this session once a transaction sits idle in it
    for `timeout`, which rolls the transaction back and lets its row locks
    go (idle_in_transaction_session_timeout, PostgreSQL 17 manual,
    19.11.1). In force at once, and for the rest of the session once the
    caller commits. set_config and not SET, because SET takes no
    parameter."""
    conn.execute(
        "SELECT set_config('idle_in_transaction_session_timeout', %s, false)",
        (str(round(timeout.total_seconds() * 1000)),),
    )


@contextmanager
def as_role(conn: psycopg.Connection[DictRow], role: str) -> Iterator[None]:
    """Run the block as one of the four NOLOGIN roles (docs/06, section 2.4).

    The connection is the login user's; SET ROLE picks the grant set for
    the stage and ingest writes, so the tests prove the grants are enough
    and a missing one shows as a permission error rather than nothing.
    RESET ROLE is skipped when the transaction has already failed, since
    it would fail too and hide the real error; the rollback ends the
    transaction and the role with it. It's skipped too when the
    connection itself is closed, `idle_in_transaction_session_timeout`
    (`bound_idle_transactions`) having ended the whole session rather
    than only the transaction (PostgreSQL 17 manual, 19.11.1): a closed
    connection reports its transaction status as UNKNOWN, not INERROR, so
    that check alone would still try RESET ROLE and raise "the connection
    is closed" over whatever the caller's own block already raised or
    returned.
    """
    conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
    try:
        yield
    finally:
        if not conn.closed and conn.info.transaction_status != TransactionStatus.INERROR:
            conn.execute("RESET ROLE")


def identity_columns(conn: psycopg.Connection[DictRow], consultation_id: UUID) -> list[DictRow]:
    """One row per identity value held for a consultation: the respondent
    it names, the column it came from, and the value itself. export.py
    calls this rather than naming the schema itself, because only
    ingest.py, stage.py and this module may (test_repo_rules.py,
    test_nothing_on_the_pipeline_path_names_the_vault); export.py is the
    one place outside that trio with a real reason to read it (docs/06,
    section 4).
    """
    return conn.execute(
        """
        SELECT v.respondent_id, v.column_ref, v.value_text
          FROM vault.respondent_identity v JOIN respondent r ON r.id = v.respondent_id
         WHERE r.consultation_id = %s
        """,
        (consultation_id,),
    ).fetchall()
