"""docs/02 step 12's XLSX export.

The file's own no-answer marker (docs/00) is a lone "-"; this module keeps
using it, since a department reviewer who has read the upload expects the
same mark to mean the same thing in the workbook they get back.
"""

from __future__ import annotations

# docs/06, section 2.7: a value starting with one of these opens as a live
# formula in Excel or LibreOffice the moment the file is opened
# (THREAT_MODEL.md, row 7).
_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
_PREFIX = "'"
NO_ANSWER = "-"


def neutralise(value: str) -> str:
    """A value starting with a formula trigger gets a leading apostrophe,
    the convention every spreadsheet reads as "force this cell to text"
    (docs/06, section 2.7). The file's own no-answer marker, a lone "-",
    is left alone: it isn't a formula wherever it's read back, and
    prefixing it would change what "not answered" looks like (docs/00).
    """
    if value == NO_ANSWER:
        return value
    if value.startswith(_TRIGGERS):
        return _PREFIX + value
    return value
