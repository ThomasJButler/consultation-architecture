# Security

## Reporting

This is a personal design repository with a small proof-of-concept. If you find
something wrong, open an issue or email the address on the profile.

## What is in scope

- The proof-of-concept under `poc/`: input handling for spreadsheets, prompt
  construction, validation of model output, logging, and the SQL.
- The CI workflow and the pre-commit configuration.

## Handling of the brief

The brief and sample data behind this work are marked OFFICIAL and are kept
outside the repository. Two guards enforce that:

1. `scripts/brief-guard.sh` (tracked) refuses any commit whose staged filenames
   or added lines match a small set of generic markers. CI runs it over the whole
   tree on every push.
2. A second script outside the repository, wired in from the local
   `.git/hooks/pre-commit`, checks staged content against the specific strings
   that must never be committed. It is not tracked because it would otherwise
   contain the very strings it guards against.

The fixtures under `poc/tests/fixtures/` describe a fictional consultation. Only
the file format is shared with the real sample.

Automated review tools that send diffs to third-party models run on `poc/`
only, never on `docs/00-brief-and-data-shape.md`.
