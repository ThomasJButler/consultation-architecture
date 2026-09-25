"""What the responses reader promises.

One row per respondent and one column per question (docs/00), read the
same way from CSV and XLSX, one row at a time, with `-` and `N/A` reaching
the validator exactly as written. The reader has no opinion about what a
cell means; that's the validator's (docs/02, section 3.2).
"""

from __future__ import annotations

import csv
import inspect
from pathlib import Path

from openpyxl import Workbook

from consult.responses import Responses, Row

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "responses.csv"
HEADER = ["respondent_ref", "d_area", "c_modes", "o_reason"]
CELLS: list[list[object]] = [
    ["R-0001", "Villages", "Cycle, Walk", "The towpath floods."],
    ["R-0002", "-", "N/A", ""],
    [42, None, "Wheelchair, mobility scooter or similar", " padded "],
]


def write_csv(path: Path) -> Path:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        for row in CELLS:
            writer.writerow(["" if cell is None else cell for cell in row])
        writer.writerow([])
    return path


def write_xlsx(path: Path) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(HEADER)
    for row in CELLS:
        sheet.append(row)
    workbook.save(path)
    return path


EXPECTED = [
    Row(
        2,
        {
            "respondent_ref": "R-0001",
            "d_area": "Villages",
            "c_modes": "Cycle, Walk",
            "o_reason": "The towpath floods.",
        },
    ),
    Row(3, {"respondent_ref": "R-0002", "d_area": "-", "c_modes": "N/A", "o_reason": ""}),
    Row(
        4,
        {
            "respondent_ref": "42",
            "d_area": "",
            "c_modes": "Wheelchair, mobility scooter or similar",
            "o_reason": "padded",
        },
    ),
]


def test_the_responses_reader_streams_rows_and_keeps_the_markers_as_written(tmp_path: Path) -> None:
    from_csv = Responses(write_csv(tmp_path / "responses.csv"))
    from_xlsx = Responses(write_xlsx(tmp_path / "responses.xlsx"))

    assert from_csv.header == from_xlsx.header == tuple(HEADER)
    assert list(from_csv.rows()) == EXPECTED
    assert list(from_xlsx.rows()) == EXPECTED
    # A generator, so a 100,000-row file is never held whole, and it can be
    # abandoned after one row without complaint.
    rows = from_csv.rows()
    assert inspect.isgenerator(rows)
    assert next(rows) == EXPECTED[0]
    rows.close()


def test_a_short_row_is_padded_and_a_long_one_is_cut_to_the_header(tmp_path: Path) -> None:
    path = tmp_path / "ragged.csv"
    path.write_text("a,b\n1\n1,2,3\n", encoding="utf-8")
    assert list(Responses(path).rows()) == [
        Row(2, {"a": "1", "b": ""}),
        Row(3, {"a": "1", "b": "2"}),
    ]


def test_the_fixture_reads_as_the_generator_wrote_it() -> None:
    responses = Responses(FIXTURE)
    rows = list(responses.rows())
    assert responses.header[0] == "respondent_ref"
    assert len(rows) == 240
    assert rows[0].no == 2
    assert rows[0].cells["respondent_ref"] == "R-0001"
    assert rows[-1].no == 241
