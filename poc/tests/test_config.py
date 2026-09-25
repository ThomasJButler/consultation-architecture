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


def test_the_caps_and_rates_come_from_settings(tmp_path: Path) -> None:
    settings = load(
        env={
            "CONSULT_DB_PASSWORD": "x",
            "CONSULT_MAX_ROWS": "12",
            "CONSULT_TOKENS_PER_ANSWER": "60",
            "CONSULT_GBP_PER_USD": "0.8",
        },
        dotenv_path=tmp_path / "absent",
    )
    assert settings.caps.max_rows == 12
    assert settings.rates.tokens_per_answer == 60
    assert settings.rates.gbp_per_usd == 0.8
    with pytest.raises(ConfigError, match="CONSULT_GBP_PER_USD"):
        load(
            env={"CONSULT_DB_PASSWORD": "x", "CONSULT_GBP_PER_USD": "free"},
            dotenv_path=tmp_path / "absent",
        )
