"""What `.env.example` and `consult.config` promise each other."""

from __future__ import annotations

from pathlib import Path

import pytest

from consult.config import EXAMPLE_PATH, SETTINGS, ConfigError, load, read_dotenv


def names_in(example: Path) -> set[str]:
    lines = example.read_text(encoding="utf-8").splitlines()
    return {line.split("=", 1)[0] for line in lines if line and not line.startswith("#")}


def test_env_example_names_every_setting() -> None:
    assert names_in(EXAMPLE_PATH) == {setting.name for setting in SETTINGS}


def test_the_environment_wins_over_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "# a comment\n\nCONSULT_DB_PASSWORD=from-file\nCONSULT_DB_PORT=6543\n", encoding="utf-8"
    )
    settings = load(env={"CONSULT_DB_PORT": "5433"}, dotenv_path=dotenv)
    assert settings.db_password == "from-file"
    assert settings.db_port == 5433
    assert settings.db_host == "127.0.0.1"


def test_a_missing_password_is_named_not_defaulted(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="CONSULT_DB_PASSWORD"):
        load(env={}, dotenv_path=tmp_path / "absent")


def test_a_dotenv_line_without_an_equals_sign_is_refused(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("CONSULT_DB_PASSWORD\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="KEY=value"):
        read_dotenv(dotenv)
