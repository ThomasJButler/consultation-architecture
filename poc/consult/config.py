"""Settings, read from the environment first and from `.env` second.

Every setting is declared once, in SETTINGS. A test holds `.env.example` to
the same list, so a setting can't be added in the code and left out of the
file a new clone copies from, or the other way round.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from consult.inputs import DEFAULT_CAPS, Caps

POC_DIR = Path(__file__).resolve().parents[1]
DOTENV_PATH = POC_DIR / ".env"
EXAMPLE_PATH = POC_DIR / ".env.example"


@dataclass(frozen=True)
class Setting:
    name: str
    default: str | None
    purpose: str


SETTINGS: tuple[Setting, ...] = (
    Setting("CONSULT_DB_HOST", "127.0.0.1", "Postgres host; loopback, per docker-compose.yml"),
    Setting("CONSULT_DB_PORT", "5432", "Postgres port"),
    Setting("CONSULT_DB_NAME", "consult", "The database `consult init` applies the schema to"),
    Setting("CONSULT_DB_USER", "consult", "The login role; the four grant roles hang off it"),
    Setting(
        "CONSULT_DB_PASSWORD",
        None,
        "Required, and blank counts as missing, so an empty password can't be the default",
    ),
    # The input guards (THREAT_MODEL.md, row 1). consult.inputs.Caps holds
    # the defaults and says what each one is for.
    Setting(
        "CONSULT_MAX_UPLOAD_BYTES", str(DEFAULT_CAPS.max_upload_bytes), "Refuse a bigger upload"
    ),
    Setting(
        "CONSULT_MAX_ROWS", str(DEFAULT_CAPS.max_rows), "Refuse a responses file with more rows"
    ),
    Setting("CONSULT_MAX_CELL_CHARS", str(DEFAULT_CAPS.max_cell_chars), "Refuse a longer cell"),
    Setting(
        "CONSULT_MAX_ZIP_RATIO", str(DEFAULT_CAPS.max_zip_ratio), "Refuse an XLSX inflating by more"
    ),
)


class ConfigError(Exception):
    """A required setting is missing or a value has the wrong shape."""


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    caps: Caps = DEFAULT_CAPS


def read_dotenv(path: Path) -> dict[str, str]:
    """Read `KEY=value` lines. Blank lines and `#` comments are skipped.

    No quoting and no interpolation, on purpose: the file holds five plain
    values and a parser that does more is a parser that surprises.
    """
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise ConfigError(f"{path}: expected KEY=value, got {line!r}")
        values[key.strip()] = value.strip()
    return values


def resolve(env: Mapping[str, str], dotenv: Mapping[str, str]) -> dict[str, str]:
    """Merge the two sources in precedence order and apply defaults."""
    resolved: dict[str, str] = {}
    for setting in SETTINGS:
        value = env.get(setting.name, dotenv.get(setting.name, setting.default))
        if value is None or value == "":
            raise ConfigError(f"{setting.name} is not set; copy .env.example to .env or export it")
        resolved[setting.name] = value
    return resolved


def _integer(values: Mapping[str, str], name: str) -> int:
    try:
        return int(values[name])
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {values[name]!r}") from exc


def load(env: Mapping[str, str] | None = None, dotenv_path: Path = DOTENV_PATH) -> Settings:
    values = resolve(os.environ if env is None else env, read_dotenv(dotenv_path))
    return Settings(
        db_host=values["CONSULT_DB_HOST"],
        db_port=_integer(values, "CONSULT_DB_PORT"),
        db_name=values["CONSULT_DB_NAME"],
        db_user=values["CONSULT_DB_USER"],
        db_password=values["CONSULT_DB_PASSWORD"],
        caps=Caps(
            max_upload_bytes=_integer(values, "CONSULT_MAX_UPLOAD_BYTES"),
            max_rows=_integer(values, "CONSULT_MAX_ROWS"),
            max_cell_chars=_integer(values, "CONSULT_MAX_CELL_CHARS"),
            max_zip_ratio=_integer(values, "CONSULT_MAX_ZIP_RATIO"),
        ),
    )
