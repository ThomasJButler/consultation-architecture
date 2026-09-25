"""The database harness: one database per session, truncated between tests,
and a blank one on request.

Tests marked `db` need a Postgres at CONSULT_DB_*; `pytest -m 'not db'` runs
without one. A missing database fails loudly rather than skipping, because a
skip in CI would hide a broken service behind a green tick.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from dataclasses import replace

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import DictRow

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


def _truncate_all(conn: psycopg.Connection[DictRow]) -> None:
    # One statement for all fourteen, so the foreign keys between them don't
    # dictate an order; RESTART IDENTITY so bigint ids read the same each test.
    tables = sql.SQL(", ").join(store.qualified(name) for name in store.TABLES)
    conn.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(tables))
    conn.commit()


@pytest.fixture(scope="session")
def db_settings() -> Iterator[Settings]:
    """One database for the whole session, with the schema applied once."""
    base = _base_settings()
    name = f"consult_test_{secrets.token_hex(4)}"
    settings = _create_database(base, name)
    with store.connect(settings) as conn:
        store.init(conn)
    yield settings
    _drop_database(base, name)


@pytest.fixture
def db(db_settings: Settings) -> Iterator[psycopg.Connection[DictRow]]:
    """A connection to the session database, every table empty on entry."""
    with store.connect(db_settings) as conn:
        _truncate_all(conn)
        yield conn
        conn.rollback()


@pytest.fixture
def blank_database() -> Iterator[Settings]:
    """A database with nothing in it, for the tests that prove `init` from empty."""
    base = _base_settings()
    name = f"consult_blank_{secrets.token_hex(4)}"
    yield _create_database(base, name)
    _drop_database(base, name)
