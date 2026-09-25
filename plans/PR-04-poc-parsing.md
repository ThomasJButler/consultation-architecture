# PR-04: Parsing and validation

**Status:** Planned   **Owner:** Thomas Butler   **Date:** 25 September 2026
**Depends on:** PR-03   **Branch:** `feat/04-poc-parsing`

## 0. What this PR does and doesn't do

**Locks in:** reading the definition workbook and the responses file into
typed structures, the validator from `docs/02` section 3.2 (errors that
block, warnings that carry a resolution, distinct values per demographic
column with counts, row and token counts, the cost estimate), the input
guards from `THREAT_MODEL.md` row 1, and `consult validate` printing the
report. Every test runs without a database.
**Doesn't yet cover:** the staging table, ingest, or anything in Postgres
(PR-06); applying a resolution (the report lists the choices; PR-06's
configure step records one per warning); the follow-up placeholder being
filled per respondent (the pipeline does that at step 6).

## 1. Objective

`consult validate tests/fixtures/responses.csv --definition
tests/fixtures/definition.xlsx` prints the report the design describes for
the fixtures: no errors; the warnings the fixtures carry on purpose
(fourteen rows of `Unsure` on a single-select question, `N/A` on a
demographic column, two options that never appear apart, three headers no
sheet mentions); the distinct values and counts; 240 rows; a token count and
a cost in pence with the assumptions printed. A hostile file is refused
before its contents are read, and nothing from a cell reaches a log line.

## 2. Methodology

Pure functions over streams. The definition parser returns a frozen
`Definition`; the responses reader yields rows through a reader that
enforces the caps as it goes; the validator consumes both and returns a
`Report` dataclass the command line prints. Multi-select cells are
tokenised against the option vocabulary by longest match, never split on
commas (`docs/02`, section 3.2). The workbook can't express an option that
contains a comma, so its naive split yields two options where one was
meant; the validator catches that as a warning of its own, two options that
never appear apart, with the resolution to merge them. The alternative, a
dataframe library, was rejected: it loads a 100,000-row file whole, hides
the row and cell caps, and brings a dependency the design doesn't need.

## 3. Test plan (defined first)

1. `test_the_definition_workbook_parses_into_three_kinds_of_question` pins
   the sheet names and headers from `docs/00`, the three response types
   spelt as `docs/02` 3.2 spells them, options split as the workbook joins
   them, and `-` or blank in `related_closed_column` meaning no related
   question.
2. `test_a_related_closed_column_must_name_a_closed_question` pins the
   error, and that a demographic or open column in that cell is an error
   too.
3. `test_an_unknown_response_type_is_an_error` pins the fixed vocabulary
   of three.
4. `test_the_responses_reader_streams_rows_and_keeps_the_markers_as_written`
   pins that CSV and XLSX read the same, that `-` and `N/A` reach the
   validator untouched, and that the reader never holds more than one row.
5. `test_multi_select_cells_are_tokenised_by_longest_match` pins
   `"Cycle, Wheelchair, mobility scooter or similar"` against a vocabulary
   that contains the comma option as three tokens, never four, and a token
   outside the vocabulary as a warning with the three resolutions.
6. `test_the_validator_reports_the_fixtures_as_designed` pins the whole
   report for the fixtures: zero errors, the warnings listed in section 1,
   the distinct values per demographic column with counts, 240 rows, the
   open-answer count, the token estimate.
7. `test_a_missing_column_the_definition_references_is_an_error`.
8. `test_a_duplicated_respondent_id_is_a_warning_with_two_resolutions`
   pins the row `docs/02` correction 8 added: ignore the column, or keep
   the id on the first occurrence and blank the rest; no row is dropped.
9. `test_options_that_never_appear_apart_are_flagged_to_merge` pins the
   comma option's warning.
10. `test_a_header_no_sheet_mentions_gets_a_role_prompt` pins the default
    roles: respondent id for a column named like an id, identity for one
    named like an email address, ignore otherwise, each overridable.
11. `test_hostile_files_are_refused_before_they_are_read` pins the guards
    in `THREAT_MODEL.md` row 1: a file over the size cap, an XLSX whose
    declared uncompressed size breaches the ratio ceiling, a cell over the
    length cap, a row count over the cap, an XLSX that isn't a zip, and an
    XML entity expansion (through `defusedxml`, which openpyxl uses when
    it is installed). Each is refused with an `ErrorCode.INPUT_INVALID`
    and a counter, never the offending content.
12. `test_the_cost_estimate_follows_docs_05` pins tokens per open answer
    times the answer count times the rate, with the rate and the tokens
    per answer as settings, so the assumptions are printed with the number.
13. `test_validate_prints_the_report_and_exits_nonzero_on_errors` pins the
    command line.
14. `test_env_example_names_every_setting` (PR-03) keeps the new cap and
    rate settings in `.env.example`.

## 4. Implementation steps

Each ends in a commit; subjects are plain sentences.

1. `Pin how the definition workbook parses` (tests 1 to 3, red)
2. `Parse the definition workbook into three kinds of question`
   (`definition.py`; green)
3. `Pin how the responses file streams` (test 4, red)
4. `Stream the responses file one row at a time` (`responses.py`, CSV and
   XLSX read-only mode; green)
5. `Pin the multi-select tokeniser` (test 5, red)
6. `Tokenise multi-select cells by longest match` (green)
7. `Pin the validator's report for the fixtures` (tests 6 to 10, red)
8. `Validate the fixtures as the design describes` (`validate.py`, the
   `Report`, the warning kinds with their resolutions; green)
9. `Pin that hostile files are refused before they are read` (test 11, red)
10. `Refuse hostile files before reading them` (the caps as settings; the
    zip central-directory check; `defusedxml` pinned with its reason; green)
11. `Pin the cost estimate against docs/05` (test 12, red)
12. `Estimate tokens and cost from the printed assumptions` (green)
13. `Pin the validate command` (test 13, red)
14. `Add the validate command` (`cli.py`; green)
15. `Say what the new test files prove` (`TESTING.md`, `poc/README.md`)
16. `Update the status block`

## 5. Output

| File | Purpose |
|---|---|
| `poc/consult/definition.py` | The workbook parser and the `Definition` types |
| `poc/consult/responses.py` | The bounded row reader for CSV and XLSX |
| `poc/consult/tokenise.py` | Longest-match tokenising against a vocabulary |
| `poc/consult/validate.py` | The validator, its `Report`, warning kinds and resolutions |
| `poc/consult/cost.py` | Token and cost estimate from settings |
| `poc/consult/config.py`, `.env.example` | The caps and the rate as settings |
| `poc/consult/cli.py` | `consult validate` |
| `poc/tests/test_definition.py`, `test_responses.py`, `test_tokenise.py`, `test_validate.py`, `test_cost.py`, `test_cli.py` | The tests above |
| `poc/tests/hostile/` | The small hostile files test 11 uses, generated by a script so nothing large is committed |
| `poc/pyproject.toml` | `defusedxml` pinned, with its one-line reason |

## 6. Security and quality notes

`THREAT_MODEL.md` row 1 is the row this pull request answers: every guard
in it is a test, and the caps are settings a reviewer can read. The report
carries column names, counts and row numbers, never a cell's text, so the
formatter from PR-03 has nothing to drop. `defusedxml` is the one new
dependency. A reviewer should read `validate.py`'s warning kinds against
the table in `docs/02` section 3.2 and its correction 8, and the tokeniser
against the comma option in the fixtures.

## 7. Fallback

If openpyxl's read-only mode can't be made to stop at the caps, read the
zip's central directory with `zipfile` first and refuse on declared sizes
before openpyxl opens anything. If the longest-match tokeniser is ambiguous
on a real vocabulary, fall back to reporting the cell as a warning with the
whole cell as one token and let the configure step resolve it.

## 8. Definition of done

- `make check` green locally and in CI; every new test runs under
  `pytest -m 'not db'`.
- All fourteen tests in section 3 exist and pass.
- `consult validate` on the fixtures prints the report in section 1.
- `README.md` Status block updated; `plans/PR-05-*.md` written;
  `RESUME.md` updated.
