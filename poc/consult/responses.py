"""The responses file, one row at a time.

One row per respondent and one column per question (docs/00), from CSV or
XLSX, read the same way: every cell is a string, `-` and `N/A` reach the
validator exactly as written, and nothing here decides what a cell means.
A generator, because a 100,000-row file can't be parsed inside a request
and shouldn't be held whole by a job either (docs/02, step 2).
"""

from __future__ import annotations

import csv
from collections.abc import Generator, Sequence
from dataclasses import dataclass
from pathlib import Path

from consult.inputs import (
    DEFAULT_CAPS,
    Caps,
    InputError,
    Refusal,
    check_cell,
    check_file,
    guarded,
    open_workbook,
)


@dataclass(frozen=True)
class Row:
    """One respondent: the file's row number (the header is row 1) and the
    cells by header, padded with empty strings and cut to the header."""

    no: int
    cells: dict[str, str]


def _cell(value: object) -> str:
    return "" if value is None else str(value).strip()


def _fitted(header: Sequence[str], values: Sequence[str]) -> dict[str, str]:
    padded = [*values, *[""] * (len(header) - len(values))]
    return dict(zip(header, padded[: len(header)], strict=True))


class Responses:
    def __init__(self, path: Path, caps: Caps = DEFAULT_CAPS) -> None:
        self.path = path
        self.caps = caps
        check_file(path, caps)
        self.header = self._read_header()

    @property
    def is_xlsx(self) -> bool:
        return self.path.suffix.lower() == ".xlsx"

    def _raw_rows(self) -> Generator[tuple[str, ...], None, None]:
        caps = self.caps
        if self.is_xlsx:
            with open_workbook(self.path, caps) as workbook:
                sheet = workbook.worksheets[0]
                for sheet_values in guarded(sheet.iter_rows(values_only=True)):
                    yield tuple(check_cell(_cell(value), caps) for value in sheet_values)
        else:
            # utf-8-sig, so a byte-order mark from a spreadsheet's CSV export
            # doesn't end up glued to the first header. The csv module's own
            # field limit stops it buffering a cell the size of the file.
            csv.field_size_limit(caps.max_cell_chars + 1)
            with self.path.open(newline="", encoding="utf-8-sig") as handle:
                try:
                    for csv_values in csv.reader(handle):
                        yield tuple(check_cell(_cell(value), caps) for value in csv_values)
                except csv.Error as exc:
                    raise InputError(Refusal.CELL_TOO_LONG, caps.max_cell_chars) from exc

    def _read_header(self) -> tuple[str, ...]:
        rows = self._raw_rows()
        try:
            return next(rows, ())
        finally:
            rows.close()

    def rows(self) -> Generator[Row, None, None]:
        raw = self._raw_rows()
        next(raw, None)
        seen = 0
        for offset, values in enumerate(raw, start=2):
            if not any(values):
                continue
            seen += 1
            if seen > self.caps.max_rows:
                raise InputError(Refusal.TOO_MANY_ROWS, self.caps.max_rows)
            yield Row(offset, _fitted(self.header, values))
