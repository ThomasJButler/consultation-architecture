"""What `export.neutralise` promises: a value that starts with a formula
trigger character is forced to text everywhere the file is opened, and the
file's own no-answer marker is never mistaken for one (docs/06, section
2.7; THREAT_MODEL.md, row 7). And what a cell written through `export._row`
holds in the sheet's own XML, which a spreadsheet reads before any rule
here applies.

Pure: no psycopg import, so this stays outside `pytest.mark.db`
(test_repo_rules.py). The workbook itself, read back cell by cell, is
`test_export.py`, marked `db` there.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from defusedxml import ElementTree
from openpyxl import Workbook

from consult import export
from consult.export import NO_ANSWER, neutralise

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def test_export_values_are_neutralised_and_a_lone_dash_is_kept() -> None:
    # Every trigger character docs/06 section 2.7 names gets a leading
    # apostrophe, the convention that forces a cell to text in Excel and
    # LibreOffice alike.
    assert neutralise("=1+1") == "'=1+1"
    assert neutralise("+44 7700 900000") == "'+44 7700 900000"
    assert neutralise("-1") == "'-1"
    assert neutralise("@mention") == "'@mention"
    assert neutralise("\tindented") == "'\tindented"
    assert neutralise("\rcarriage") == "'\rcarriage"

    # A lone "-" is docs/00's own no-answer marker, never a formula
    # wherever it's read back, so the rule leaves it exactly as it is.
    assert neutralise(NO_ANSWER) == NO_ANSWER
    assert neutralise("-") == "-"

    # A trigger character after the first position doesn't make a formula
    # in any spreadsheet reader, so a value carrying one is left alone.
    assert neutralise("safe-value") == "safe-value"
    assert neutralise("2 + 2 = 4") == "2 + 2 = 4"

    # Nothing to neutralise in an empty cell.
    assert neutralise("") == ""


def _decoded(text: str) -> str:
    # ECMA-376 Part 1, 22.9.2.19 (ST_Xstring): _xHHHH_ in a cell's text is
    # the character with that code, read left to right. openpyxl's own
    # reader doesn't decode it; Excel does, on open.
    return re.sub("_x([0-9A-Fa-f]{4})_", lambda match: chr(int(match.group(1), 16)), text)


def test_an_xhhhh_escape_reaches_the_workbook_as_typed(tmp_path: Path) -> None:
    # An answer typed as _x003D_HYPERLINK(...) opens in Excel as
    # =HYPERLINK(...), a trigger neutralise never saw, since openpyxl
    # 3.1.5 writes the text as given. The escape itself has to be escaped:
    # its underscore written as _x005F_, the form XlsxWriter writes. The
    # second value is two escapes sharing an underscore, where escaping
    # only the first match leaves the second to decode.
    typed = ['_x003D_HYPERLINK("https://example.org","open")', "_x0041_x0042_"]
    workbook = Workbook(write_only=True)
    ws = workbook.create_sheet("Responses")
    values: list[object] = [*typed]
    export._row(ws, values)
    path = tmp_path / "escape.xlsx"
    workbook.save(path)

    with zipfile.ZipFile(path) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "_x005F_x003D_" in sheet
    assert re.search("(?<!_x005F)_x003D_", sheet) is None
    texts = [node.text or "" for node in ElementTree.fromstring(sheet).iter(f"{_MAIN}t")]
    assert [_decoded(text) for text in texts] == typed
