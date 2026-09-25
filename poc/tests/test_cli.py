"""What `consult init` promises: the schema in docs/04, applied to a database."""

from __future__ import annotations

import pytest

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
    assert {"vault", "staging"} <= schemas_in(blank_database)
    assert DESIGN_ROLES <= roles_in(blank_database)

    # Running it twice is harmless: a second `init` on a live database
    # creates nothing and drops nothing.
    assert main(["init"], settings=blank_database) == 0
    assert tables_in(blank_database) == DESIGN_TABLES
