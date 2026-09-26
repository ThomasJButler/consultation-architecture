"""Pins that TESTING.md names a proving test for each of the four
mechanics the design rests on (docs/02, section 13), not just a file, so
a reader can run the one test that proves a claim rather than take the
file's word for it (CLAUDE.md rule 10: proved by tests, not asserted).
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
TESTING_MD = TESTS_DIR.parent / "TESTING.md"

# docs/02 section 13's own wording, in its own order, so a failure names
# the mechanic that lacks a named test.
MECHANICS = [
    "the fan-in transaction",
    "lease takeover with a fence",
    "idempotent tag inserts",
    "the indexed filter query",
]

REF_RE = re.compile(r"`(test_[A-Za-z0-9_]+\.py(?:::test_[A-Za-z0-9_]+)?)`")
FUNCTION_RE = re.compile(r"::(test_[A-Za-z0-9_]+)$")


def _mechanics_paragraph(text: str) -> str:
    for paragraph in text.split("\n\n"):
        if paragraph.lstrip().startswith("The four mechanics the design rests on"):
            return " ".join(paragraph.split())
    raise AssertionError("TESTING.md has no paragraph on the four mechanics")


def test_testing_md_names_a_test_for_each_mechanic() -> None:
    paragraph = _mechanics_paragraph(TESTING_MD.read_text(encoding="utf-8"))
    starts = [paragraph.find(name) for name in MECHANICS]
    for name, start in zip(MECHANICS, starts, strict=True):
        assert start >= 0, f"{name}: not named in the paragraph on the four mechanics"

    for position, name in enumerate(MECHANICS):
        start = starts[position]
        # A mechanic's reference has to sit in its own sentence: the span
        # ends at the next mechanic or at the sentence's full stop,
        # whichever comes first, so a reference later in the paragraph
        # (the vault test's, say) can't stand in for the last mechanic's.
        sentence_end = paragraph.find(". ", start)
        if sentence_end < 0:
            sentence_end = len(paragraph)
        next_start = starts[position + 1] if position + 1 < len(starts) else len(paragraph)
        span = paragraph[start : min(sentence_end, next_start)]
        match = REF_RE.search(span)
        assert match is not None, f"{name}: no test reference in its sentence"
        ref = match.group(1)
        function_match = FUNCTION_RE.search(ref)
        assert function_match is not None, (
            f"{name}: {ref!r} names a file, not a test (want file.py::test_name)"
        )
        file_name, function_name = ref.split("::", 1)
        path = TESTS_DIR / file_name
        assert path.is_file(), f"{name}: {file_name} does not exist under poc/tests/"
        source = path.read_text(encoding="utf-8")
        assert f"def {function_name}(" in source, (
            f"{name}: {function_name} is not defined in {file_name}"
        )
