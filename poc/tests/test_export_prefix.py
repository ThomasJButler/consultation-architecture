"""What `export.neutralise` promises: a value that starts with a formula
trigger character is forced to text everywhere the file is opened, and the
file's own no-answer marker is never mistaken for one (docs/06, section
2.7; THREAT_MODEL.md, row 7).

Pure: no psycopg import, so this stays outside `pytest.mark.db`
(test_repo_rules.py). The workbook itself, read back cell by cell, is
`test_export.py`, marked `db` there.
"""

from __future__ import annotations

from consult.export import NO_ANSWER, neutralise


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
