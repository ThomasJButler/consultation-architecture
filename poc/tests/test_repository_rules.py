"""Pins CLAUDE.md rule 12 over the tracked text of the repository: nothing
names the recruitment process. The brief and the sample data stay outside
the tree (rule 1); this checks the words about them, which the handling
note in README.md and .gitignore carry, and the paths the plans give.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# The phrases that name the process rather than the brief. CLAUDE.md
# states the rule in these words and plans/PR-10 records the question that
# led here, so both are read past; this file names them to test for them;
# .gitignore and scripts/brief-guard.sh have to name the brief's own
# markers to keep it out, the exception the guard makes for itself.
NAMES_THE_PROCESS = re.compile(r"recruitment process|take[-_ ]?home", re.IGNORECASE)
READ_PAST = {
    Path("CLAUDE.md"),
    Path("plans/PR-10-submission-polish.md"),
    Path("poc/tests/test_repository_rules.py"),
    Path(".gitignore"),
    Path("scripts/brief-guard.sh"),
}
TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".sql",
    ".sh",
    ".yml",
    ".yaml",
    ".toml",
    ".txt",
    ".html",
    ".mmd",
    ".cfg",
    ".ini",
}
TEXT_NAMES = {".gitignore", ".env.example", "Makefile", ".pre-commit-config.yaml"}
SKIPPED_DIRS = {
    ".git",
    ".venv",
    ".build-venv",
    "node_modules",
    "__pycache__",
    ".claude",
    "exports",
    "out",
}


def _tracked_text_files() -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        if any(
            part in SKIPPED_DIRS or (part.startswith(".") and part not in TEXT_NAMES)
            for part in path.relative_to(ROOT).parts[:-1]
        ):
            continue
        if path.is_file() and (path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES):
            files.append(path)
    return files


def test_no_file_names_the_recruitment_process() -> None:
    # Whitespace is folded first: a phrase wrapped across two lines of a
    # paragraph is still the phrase.
    offenders = []
    for path in _tracked_text_files():
        relative = path.relative_to(ROOT)
        if relative in READ_PAST:
            continue
        text = " ".join(path.read_text(encoding="utf-8", errors="replace").split())
        if NAMES_THE_PROCESS.search(text):
            offenders.append(str(relative))
    assert offenders == [], f"rule 12: {', '.join(offenders)}"
