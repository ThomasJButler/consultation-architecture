"""The input guards: a hostile file is refused before its contents are read.

THREAT_MODEL.md row 1: an XLSX is a zip of XML, so one file can be a zip
bomb or an entity-expansion attack, a CSV can carry one cell the size of
the file, and a file can just be too big. Every guard here answers one of
those with a refusal that carries a reason and a count and never the
content, so nothing from a cell reaches a log line or an error message
(section 2 of the same file). The caps are settings (consult.config), so
a reviewer can read them and the first real upload can move them.

defusedxml does the entity work: openpyxl parses through it whenever it's
installed, and forbids the entity declarations an expansion attack needs.
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from defusedxml import DefusedXmlException
from openpyxl import load_workbook
from openpyxl.workbook.workbook import Workbook

from consult.errors import ErrorCode


class Refusal(StrEnum):
    TOO_LARGE = "too_large"
    ZIP_RATIO = "zip_ratio"
    NOT_A_ZIP = "not_a_zip"
    XML_FORBIDDEN = "xml_forbidden"
    CELL_TOO_LONG = "cell_too_long"
    TOO_MANY_ROWS = "too_many_rows"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Caps:
    """The limits, with defaults that admit the largest consultations docs/02
    section 1 assumes (over 100,000 responses) and refuse the absurd."""

    max_upload_bytes: int = 200 * 1024 * 1024
    max_rows: int = 250_000
    max_cell_chars: int = 20_000
    max_zip_ratio: int = 100


DEFAULT_CAPS = Caps()


class InputError(Exception):
    """A refusal: which guard, and a number. Never the content."""

    code = ErrorCode.INPUT_INVALID

    def __init__(self, reason: Refusal, count: int) -> None:
        self.reason = reason
        self.count = count
        super().__init__(f"{reason.value} ({count})")


def check_file(path: Path, caps: Caps) -> None:
    """The checks that need no parsing: size, and for a zip, what it declares."""
    size = path.stat().st_size
    if size > caps.max_upload_bytes:
        raise InputError(Refusal.TOO_LARGE, size)
    if path.suffix.lower() != ".xlsx":
        return
    try:
        with zipfile.ZipFile(path) as archive:
            declared = sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile as exc:
        raise InputError(Refusal.NOT_A_ZIP, 0) from exc
    # The central directory says how big each entry claims to be once
    # inflated; a bomb claims hundreds of times its own size.
    ratio = declared // max(size, 1)
    if ratio > caps.max_zip_ratio:
        raise InputError(Refusal.ZIP_RATIO, ratio)


def _forbidden_xml(exc: BaseException | None) -> bool:
    """openpyxl wraps a parse failure in its own ValueError with the cause
    chained, so the defusedxml refusal has to be looked for down the chain."""
    while exc is not None:
        if isinstance(exc, DefusedXmlException):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _refusal(exc: BaseException) -> InputError:
    reason = Refusal.XML_FORBIDDEN if _forbidden_xml(exc) else Refusal.UNREADABLE
    return InputError(reason, 0)


@contextmanager
def open_workbook(path: Path, caps: Caps) -> Iterator[Workbook]:
    """A read-only workbook whose file handle is ours, so a load that fails
    halfway (which is what a hostile file produces) can't leak it."""
    check_file(path, caps)
    with path.open("rb") as handle:
        try:
            workbook = load_workbook(handle, read_only=True, data_only=True)
        except (zipfile.BadZipFile, KeyError, ValueError) as exc:
            raise _refusal(exc) from exc
        try:
            yield workbook
        finally:
            workbook.close()


def guarded[T](rows: Iterator[T]) -> Iterator[T]:
    """A sheet's XML is parsed as it's iterated, so the entity check can
    fire mid-stream; this turns it into the same refusal either way."""
    try:
        yield from rows
    except ValueError as exc:
        raise _refusal(exc) from exc


def check_cell(cell: str, caps: Caps) -> str:
    if len(cell) > caps.max_cell_chars:
        raise InputError(Refusal.CELL_TOO_LONG, len(cell))
    return cell
