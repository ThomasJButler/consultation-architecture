"""What `consult init` promises: the schema in docs/04, applied to a database."""

from __future__ import annotations

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import DictRow

from consult import store
from consult.cli import main
from consult.config import Settings
from consult.store import connect
from tests.rows import make_consultation, make_department, make_running_job

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
    # creates nothing, drops nothing and keeps the rows that were there.
    with connect(blank_database) as conn:
        department_id = make_department(conn)
        conn.commit()
    assert main(["init"], settings=blank_database) == 0
    assert tables_in(blank_database) == DESIGN_TABLES
    with connect(blank_database) as conn:
        row = conn.execute(
            "SELECT count(*) AS departments FROM department WHERE id = %s", (department_id,)
        ).fetchone()
    assert row == {"departments": 1}


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


def oids_of(settings: Settings) -> dict[str, int]:
    """Each table's oid: a dropped and recreated table gets a new one."""
    with connect(settings) as conn:
        rows = conn.execute(
            "SELECT name, to_regclass(name)::oid AS oid FROM unnest(%s::text[]) AS t(name)",
            (sorted(DESIGN_TABLES),),
        ).fetchall()
    oids = {row["name"]: row["oid"] for row in rows}
    assert all(oid is not None for oid in oids.values())
    return oids


def row_counts_of(settings: Settings) -> dict[str, int]:
    counts: dict[str, int] = {}
    with connect(settings) as conn:
        for name in DESIGN_TABLES:
            row = conn.execute(
                sql.SQL("SELECT count(*) AS rows FROM {}").format(store.qualified(name))
            ).fetchone()
            assert row is not None
            counts[name] = row["rows"]
    return counts


def test_init_reset_leaves_the_same_empty_schema(
    db: psycopg.Connection[DictRow], db_settings: Settings
) -> None:
    # Rows in three tables, so a reset that forgot one would show.
    consultation_id = make_consultation(db, make_department(db))
    make_running_job(db, consultation_id)
    db.commit()
    before = shape_of(db_settings)
    oids_before = oids_of(db_settings)

    assert main(["init", "--reset"], settings=db_settings) == 0

    assert shape_of(db_settings) == before
    assert tables_in(db_settings) == DESIGN_TABLES
    oids_after = oids_of(db_settings)
    assert all(oids_after[name] != oids_before[name] for name in DESIGN_TABLES)
    assert row_counts_of(db_settings) == dict.fromkeys(DESIGN_TABLES, 0)


def test_the_drop_and_truncate_list_names_the_design_tables() -> None:
    # store.TABLES drives both reset and the harness's truncation, so it
    # can't be allowed to drift from what schema.sql creates.
    qualified = {name if "." in name else f"public.{name}" for name in store.TABLES}
    assert qualified == DESIGN_TABLES
