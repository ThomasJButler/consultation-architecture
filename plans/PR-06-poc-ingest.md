# PR-06: Ingest

**Status:** Planned   **Owner:** Thomas Butler   **Date:** 25 September 2026
**Depends on:** PR-05   **Branch:** `feat/06-poc-ingest`

## 0. What this PR does and doesn't do

**Locks in:** the path from a validated file to rows in the schema: the
`stage` step that copies the file into a logged table in the `staging`
schema (docs/02, step 2; docs/04, section 2), the configure step that
turns the definition, the report and the resolutions into `question`,
`question_option`, `column_roles` and `value_policy` rows (step 3), and
the `ingest` step that explodes answers to the long table, builds
`respondent.attrs`, sends identity columns to the vault, flags duplicates
at both levels, inserts one `find_themes` job per open question and sets
the consultation processing, in one transaction (step 3a; ADR-004). Plus
the vault test `docs/06` section 2.4 promises: the pipeline role is
refused at the vault, and nothing on the pipeline path names the schema.
A `consult ingest` command runs the three steps in turn for a file, so
the proof-of-concept runs end to end from a spreadsheet to a schema full
of rows, with no model yet.
**Doesn't yet cover:** dispatch (the jobs go in `pending`), the worker and
the reconciler (PR-08), any model call (PR-07), the filter query (PR-09).
Two things the design has here are deferred with named reasons: the
`stage` and `ingest` job rows docs/02 steps 2 and 3 have Confirm insert
go in with dispatch (PR-08), so the steps run inline and the edges in
`transitions.py` are the record of them; and re-running stage from the
stored upload when the table is missing (docs/02, correction 5) needs an
upload store this proof-of-concept hasn't got, so ingest refuses by name
instead. The rows are held and written with `executemany` rather than
streamed with COPY, which the code says at the point it does it.

## 1. Objective

`consult ingest tests/fixtures/responses.csv --definition
tests/fixtures/definition.xlsx --name "Riverside cycle route"
--department "Fictional Affairs"` leaves the schema holding 240
respondents, the answer rows docs/04 section 7 predicts for them (one per
respondent per question, one per chosen option for the multi-select,
blanks counted), 240 `attrs` documents that match the answer rows, the
email addresses in the vault and nowhere else, the twelve proforma rows
flagged as duplicates at both levels, two `find_themes` jobs, and a
consultation in `processing` with its staging table gone.

## 2. Methodology

One module per step, each a function over one connection that never
commits: `consult/stage.py`, `consult/configure.py`, `consult/ingest.py`.
Stage uses psycopg's COPY to load the file's rows into a table named by
the consultation id, created with `sql.Identifier`, every column text
plus the file's row number. Configure takes the validator's report and a
`Resolutions` value (defaults: every unknown value mapped to nothing and
left as not answered, `N/A` kept, the never-apart pair merged, roles as
suggested) and writes the rows the schema needs. Ingest reads the staging
table in row order and writes respondents, vault rows, answers and attrs
with `ON CONFLICT DO NOTHING` on the keys docs/04 section 3 names, so a
second delivery of the ingest message finds the rows already there;
duplicates are computed by SQL over the rows just written (same question,
same `text_sha256` for answers; every open answer identical for
respondents), never deleted. The consultation's status changes go through
`transitions.py`, which gains the three early edges, so the repo rule
stands. Roles: the connection runs as the login user and does `SET ROLE
consult_ingest` around the stage and ingest writes, and the vault test
does `SET ROLE consult_pipeline`. The alternative, one big `ingest()`
that also parses and validates, was rejected because the validator has
to run before spend and before configure, not inside ingest.

## 3. Test plan (defined first)

1. `test_stage_copies_the_file_into_a_logged_staging_table` pins the
   table in the `staging` schema, `relpersistence = 'p'`, one row per file
   row with the file's row number, `upload_sha256` and `row_count` on the
   consultation, and the consultation `staged`.
2. `test_configure_writes_questions_options_roles_and_policies` pins the
   fixture's questions as rows (`kind`, `response_type`, `ordinal`,
   `related_closed_question_id`), options as rows with the comma option
   merged back into one, `column_roles` naming the id and ignored columns,
   `value_policy` carrying the `N/A` decision and the `Unsure` mapping.
3. `test_ingest_explodes_answers_one_row_per_option` pins the long table:
   one row per respondent per question, one per chosen option for the
   multi-select, `is_blank` rows for `-` and blank cells, open answers
   with `text_sha256`, and the unique key holding.
4. `test_ingest_builds_attrs_that_match_the_answer_rows` pins docs/04
   section 5 (values always arrays, `N/A` kept, a skipped column absent)
   and ADR-004's promised check: every respondent's `attrs` rebuilt from
   their answer rows equals what ingest wrote.
5. `test_ingest_flags_duplicates_at_answer_and_respondent_level` pins the
   proforma: `duplicate_of_answer_id` on every copy but the first,
   `respondent.duplicate_of` on the twelve, nothing deleted, counts
   readable both ways.
6. `test_identity_columns_go_to_the_vault_and_nowhere_else` pins the
   email in `vault.respondent_identity` and absent from `answer`,
   `attrs` and `question_text`.
7. `test_the_pipeline_role_cannot_read_the_vault` pins the refusal (`SET
   ROLE consult_pipeline`, then `InsufficientPrivilege`) and, as a repo
   rule, that no module outside `ingest.py` and `stage.py` contains
   `vault.`.
8. `test_ingest_inserts_one_find_themes_job_per_open_question` pins two
   `pending` jobs carrying the consultation's `run_id`, the consultation
   `processing`, and the staging table dropped.
9. `test_ingest_is_idempotent_on_replay` pins that running ingest twice
   over the same staging table leaves the same rows, jobs and attrs.
10. `test_a_repeated_respondent_id_is_refused_by_ingest_unless_resolved`
    pins docs/04 section 3: ingest refuses rather than guesses, and the
    `keep_first_blank_rest` resolution lets it through.
11. `test_the_ingest_command_runs_the_fixtures_end_to_end` pins the
    command's counts and exit code, and that its output carries counts
    and ids and no answer text.

## 4. Implementation steps

Each ends in a commit; subjects are plain sentences; one failing test per
`Pin ...`.

1. `Pin that stage copies the file into a logged staging table` (test 1)
2. `Copy the file into a staging table with COPY` (`stage.py`; the
   `draft`, `staging` and `staged` edges in `transitions.py`)
3. `Pin what configure writes` (test 2)
4. `Write questions, options, roles and policies from the report`
   (`configure.py`)
5. `Pin that ingest explodes answers one row per option` (test 3)
6. `Explode answers into the long table` (`ingest.py`, first half)
7. `Pin that attrs match the answer rows` (test 4)
8. `Build attrs from the answer rows` (green)
9. `Pin the duplicate flags at both levels` (test 5)
10. `Flag duplicates by sha256 and never delete them` (green)
11. `Pin that identity goes to the vault and nowhere else` (test 6)
12. `Send identity columns to the vault as the ingest role` (green)
13. `Pin that the pipeline role cannot read the vault` (test 7)
14. `Pin the find_themes jobs and the processing edge` (test 8)
15. `Insert the find_themes jobs and set the consultation processing`
    (green, with the `staged` to `processing` edge in `transitions.py`)
16. `Pin that ingest is idempotent on replay` (test 9)
17. `Make ingest a no-op the second time` (green if the keys do it)
18. `Pin that a repeated respondent id is refused unless resolved` (test 10)
19. `Refuse a repeated respondent id unless the resolution says otherwise`
20. `Pin the ingest command` (test 11)
21. `Add the ingest command` (`cli.py`)
22. `Say what the new test files prove` (`TESTING.md`, `poc/README.md`)
23. `Update the status block`

## 5. Output

| File | Purpose |
|---|---|
| `poc/consult/stage.py` | The staging table and the COPY |
| `poc/consult/configure.py` | Questions, options, roles and policies from the report and the resolutions |
| `poc/consult/ingest.py` | The long table, attrs, the vault, duplicates, the jobs |
| `poc/consult/transitions.py` | The `draft` to `processing` edges |
| `poc/consult/cli.py` | `consult ingest` |
| `poc/tests/test_stage.py`, `test_configure.py`, `test_ingest.py`, `test_vault.py`, `test_cli_ingest.py` | The tests above |

## 6. Security and quality notes

`THREAT_MODEL.md` row 6 is the row this pull request answers: the vault in
its own schema, the pipeline role with no grant on it, and a test that
proves the refusal rather than asserting it. Every identifier built from
a consultation id goes through `sql.Identifier`; every value is a
parameter or a COPY row. The `ingest` command prints counts and ids, and
the log lines carry consultation, question and job ids and row counts
through `logs.log_event`; no answer or identity value reaches either. A
reviewer should read `ingest.py`'s attrs builder against docs/04 section
5, the duplicate SQL against docs/02 section 7 decision 9, and the two
`SET ROLE` statements against `docs/06` section 2.4.

## 7. Fallback

If COPY through psycopg proves awkward with a dynamic column list, insert
rows in batches of a thousand with `executemany`; the staging table's
shape is the same. If `SET ROLE` inside a transaction complicates the
harness, run the vault test on its own connection.

## 8. Definition of done

- `make check` green locally and in CI.
- All eleven tests in section 3 exist and pass.
- `consult ingest` on the fixtures leaves the schema as section 1 describes.
- `README.md` Status block updated; `plans/PR-07-*.md` written;
  `RESUME.md` updated.
