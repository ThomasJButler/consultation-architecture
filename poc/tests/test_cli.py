"""What `consult init` promises: the schema in docs/04, applied to a database."""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import DictRow

from consult.cli import main
from consult.config import Settings
from consult.store import connect

pytestmark = pytest.mark.db

# docs/04, section 8: fourteen of the sixteen tables, the vault in its own
# schema. `export` and `audit_event` stay in the design and out of the
# proof-of-concept.
DESIGN_TABLES = {
    "public.department",
    "public.consultation",
    "public.question",
    "public.question_option",
    "public.respondent",
    "vault.respondent_identity",
    "public.answer",
    "public.theme_set_version",
    "public.theme",
    "public.theme_example",
    "public.answer_theme",
    "public.job",
    "public.job_batch",
    "public.notification_outbox",
}

# docs/06, section 2.4 as corrected: four roles, cluster-wide.
DESIGN_ROLES = {"consult_ingest", "consult_pipeline", "consult_export", "consult_admin"}


def tables_in(settings: Settings) -> set[str]:
    with connect(settings) as conn:
        rows = conn.execute(
            """
            SELECT table_schema || '.' || table_name AS name
              FROM information_schema.tables
             WHERE table_schema IN ('public', 'vault') AND table_type = 'BASE TABLE'
            """
        ).fetchall()
    return {row["name"] for row in rows}


def schemas_in(settings: Settings) -> set[str]:
    with connect(settings) as conn:
        rows = conn.execute("SELECT nspname AS name FROM pg_namespace").fetchall()
    return {row["name"] for row in rows}


def roles_in(settings: Settings) -> set[str]:
    with connect(settings) as conn:
        rows = conn.execute("SELECT rolname AS name FROM pg_roles").fetchall()
    return {row["name"] for row in rows}


def test_init_creates_every_table_the_design_names(blank_database: Settings) -> None:
    assert tables_in(blank_database) == set()

    assert main(["init"], settings=blank_database) == 0

    assert tables_in(blank_database) == DESIGN_TABLES
    assert schemas_in(blank_database) >= {"vault", "staging"}
    assert roles_in(blank_database) >= DESIGN_ROLES

    # Running it twice is harmless: a second `init` on a live database
    # creates nothing and drops nothing.
    assert main(["init"], settings=blank_database) == 0
    assert tables_in(blank_database) == DESIGN_TABLES


def shape_of(settings: Settings) -> set[tuple[object, ...]]:
    """Every column and every index, so "the same schema" means the same DDL."""
    with connect(settings) as conn:
        columns = conn.execute(
            """
            SELECT table_schema, table_name, column_name, data_type, is_nullable,
                   column_default, identity_generation
              FROM information_schema.columns
             WHERE table_schema IN ('public', 'vault')
            """
        ).fetchall()
        indexes = conn.execute(
            """
            SELECT schemaname, tablename, indexname, indexdef
              FROM pg_indexes
             WHERE schemaname IN ('public', 'vault')
            """
        ).fetchall()
    return {tuple(row.values()) for row in columns} | {tuple(row.values()) for row in indexes}


def test_init_reset_leaves_the_same_empty_schema(
    db: psycopg.Connection[DictRow], db_settings: Settings
) -> None:
    db.execute("INSERT INTO department (name) VALUES ('Department of Fictional Affairs')")
    db.commit()
    before = shape_of(db_settings)

    assert main(["init", "--reset"], settings=db_settings) == 0

    assert shape_of(db_settings) == before
    assert tables_in(db_settings) == DESIGN_TABLES
    with connect(db_settings) as conn:
        row = conn.execute("SELECT count(*) AS departments FROM department").fetchone()
    assert row is not None
    assert row["departments"] == 0
