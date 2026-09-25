"""The database harness: one database per session, a blank one on request.

Tests marked `db` need a Postgres at CONSULT_DB_*; `pytest -m 'not db'` runs
without one. A missing database fails loudly rather than skipping, because a
skip in CI would hide a broken service behind a green tick.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from dataclasses import replace

import pytest
from psycopg import sql

from consult import config, store
from consult.config import Settings


def _base_settings() -> Settings:
    return config.load()


def _create_database(base: Settings, name: str) -> Settings:
    # CREATE DATABASE can't run in a transaction, hence autocommit, and can't
    # take a parameter, hence a composed identifier rather than a format string.
    with store.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return replace(base, db_name=name)


def _drop_database(base: Settings, name: str) -> None:
    with store.connect(base, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


@pytest.fixture
def blank_database() -> Iterator[Settings]:
    """A database with nothing in it, for the tests that prove `init` from empty."""
    base = _base_settings()
    name = f"consult_blank_{secrets.token_hex(4)}"
    yield _create_database(base, name)
    _drop_database(base, name)
