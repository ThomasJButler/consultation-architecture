"""Connections and the schema. Every SQL statement the design rests on lives
here or in a sibling module, parameterised and readable (CLAUDE.md, rule 10).
"""

from __future__ import annotations

from datetime import timedelta
from importlib import resources
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row

from consult.config import Settings
from consult.errors import ErrorCode

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


def record_failure(
    conn: psycopg.Connection[DictRow],
    job_id: UUID,
    *,
    claimed_by: str,
    fence: int,
    error_code: ErrorCode,
    provider_request_id: str | None = None,
    retry_in: timedelta = timedelta(minutes=1),
) -> bool:
    """Record a failure as a code and a request id, never a message.

    Guarded by the fence like every worker write (ADR-002): a worker whose
    lease was taken over writes nothing and learns so from the False. The
    job goes to failed_retryable with a time to retry; whether it retries or
    is marked failed at five attempts is the reconciler's call (docs/02,
    section 5). Nothing here commits: the caller owns the transaction.
    """
    cursor = conn.execute(
        """
        UPDATE job
           SET status = 'failed_retryable',
               error_code = %(error_code)s,
               provider_request_id = %(provider_request_id)s,
               next_attempt_at = now() + %(retry_in)s,
               heartbeat_at = now()
         WHERE id = %(job_id)s
           AND claimed_by = %(claimed_by)s
           AND attempts = %(fence)s
           AND status = 'running'
        """,
        {
            "job_id": job_id,
            "claimed_by": claimed_by,
            "fence": fence,
            "error_code": error_code.value,
            "provider_request_id": provider_request_id,
            "retry_in": retry_in,
        },
    )
    return cursor.rowcount == 1
