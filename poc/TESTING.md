# Testing

What each test file proves, how the harness works, and how to run it. The
rule is CLAUDE.md rule 2: a behaviour gets a red commit that pins it before
the green commit that makes it pass. The exceptions, each named in its
commit body: the settings tests went in green with the settings they check
(PR-03's project, PR-04's caps and rates), the repo rules with the
documents, and PR-05's named proofs (the stale-lease takeover, the
positive-list predicate, the fan-in race pair) after the code they prove,
as that plan said they might, and PR-06's vault refusal, which passes on
the grants PR-03 wrote and is pinned so a later `GRANT` turns it red.
PR-06's review round also added pins that were green on arrival, each
saying so in its commit body: the vault repo rule (a source grep, red
only when a module outside `ingest.py`, `stage.py` and `store.py` names
the schema), the refusals of the three early edges and the stage
backstop, the CLI's rollback branch, the two resolutions the defaults
never reach, and the redelivery after the consultation has moved on.
Every other test here has a `Pin ...` commit ahead of the code it holds.

## Running

```bash
cd poc
cp .env.example .env            # then change the password
docker compose up -d db         # Postgres 17 on 127.0.0.1
make venv                       # Python 3.12 venv with every tool pinned
make check                      # ruff, mypy, pytest with coverage, pip-audit, bandit
make test-pure                  # pytest -m 'not db': no Docker needed
```

CI runs `make check` on Python 3.12 against a Postgres 17 service container
(`.github/workflows/ci.yml`). A pull request's head has to be green; a red
test commit in its history is expected.

## The harness (`tests/conftest.py`)

- `db_settings` (session): one database, `consult_test_<hex>`, created on
  the configured server with the schema applied once, dropped at the end.
- `db` (function): a connection to it, with all fourteen tables truncated in
  one statement on entry so a crashed test can't leak rows into the next.
- `blank_database` (function): an empty database with no schema, for the
  tests that prove `init` from nothing.
- A missing database fails loudly rather than skipping. A skip in CI would
  hide a broken service behind a green tick.

Markers: `db` for anything that needs Postgres, `slow` for the tests later
pull requests add that take more than a few seconds. `pytest.ini` sets
`--strict-markers`, so a misspelt marker is an error.

## What each file proves

| File | Proves | Needs |
|---|---|---|
| `test_config.py` | `.env.example` and `consult/config.py` name the same settings; the environment wins over `.env`; a missing password is named, not defaulted; the input caps and the cost rates load from the environment and a rate that isn't a number is refused by name | nothing |
| `test_cli.py` | `consult init` creates the fourteen tables, the `vault` and `staging` schemas and the four roles from `docs/04` and `docs/06`, and running it twice is harmless; `--reset` drops and recreates the same schema with no rows surviving | Postgres |
| `test_fixtures.py` | The generator writes the three sheets with the headers in `docs/00`, comma-joined options including one containing a comma, one follow-up question with a placeholder, `-` and `N/A`; the committed fixtures are byte for byte what it writes | nothing |
| `test_fakes.py` | `RecordingLLM` keeps every prompt and answers well; `FakeLLM` answers with what it was told to, including each fault in `THREAT_MODEL.md` row 3, and raises when its script runs out | nothing |
| `test_store.py` | A failed job stores an error code and a provider request id; every column of `job` that can hold a string is on a named allow-list and `params` is held to a JSON object; the code vocabulary is a `CHECK` that names exactly the enum's values; a stale fence writes nothing | Postgres |
| `test_logs.py` | The formatter keeps ids, counts, durations, states and codes and drops everything else by name and by shape; a sentence as a message becomes a marker; an exception contributes its class and never its message | nothing |
| `test_repo_rules.py` | Every test module that needs a database is marked `db`, and only those; nothing outside `transitions.py` writes `consultation.status`; nothing on the pipeline path names the `vault` schema | nothing |
| `test_jobs.py` | The claim returns the fence and refuses a live lease; a lease stale for ten minutes can be taken over and the fence moves on; a zombie's heartbeat, checkpoint and failure record are all refused, and the checkpoint INSERT refuses a stale fence on its own without the heartbeat in front; checkpoints are idempotent and a worker resumes from the last one | Postgres |
| `test_transitions.py` | Fan-in 1 flips the consultation behind the row lock and writes one `themes_ready` row naming the pass; its predicate waits for configured and finding questions and not for failed or signed-off ones; fan-in 2 needs every question complete; the sign-off guard admits one reviewer, freezes v2 with the fallbacks and queues one map job; a reopen mints a run id so the second email has its own row; every transition stamps `status_changed_at`; a reopen of a consultation that isn't ready and a second finish of a question already moved on both refuse and change nothing; the draft-to-staging and staging-to-staged edges refuse the wrong state and the processing edge answers False | Postgres |
| `test_fan_in_race.py` | Twenty threaded finishers on twenty connections flip the consultation exactly once with one email row (marked `slow`); and without the row lock two finishers lose the update, hand-stepped on two connections, which the reconciler's fourth statement then frees | Postgres |
| `test_tags.py` | A replayed batch inserts nothing, a retracted tag stays retracted through the replay, a human re-add clears the retraction rather than duplicating the row; a theme from another version or an answer from another question or department is refused, by the worker's insert and the human one; and the INSERT carries the fence itself | Postgres |
| `test_definition.py` | The workbook parses into the three kinds of question in `docs/00`; the three response types are the vocabulary `docs/02` 3.2 names; `-` or blank means no related question; every problem is reported together and a bad response type doesn't cascade into a second problem | nothing |
| `test_responses.py` | CSV and XLSX read the same, one row at a time (the first row arrives before the row cap refuses the second), with `-` and `N/A` kept as written; short rows are padded and long ones cut to the header; the csv module's field limit is put back after a read | nothing |
| `test_tokenise.py` | A multi-select cell is matched by longest match against the vocabulary, never split on commas, and a token outside it is reported rather than guessed | nothing |
| `test_validate.py` | The whole report for the fixtures, with counts read from the CSV: no errors; the `Unsure` rows (default: not answered, the one resolution that needs no choice), the `N/A` column, the two options that never appear apart (and only when they sit together in the option list), the three unmatched headers with their default roles however the header is spelt; a missing, repeated or blank column is an error, and so is a header over 63 bytes, which Postgres would truncate; a repeated respondent id is a warning with two resolutions and its row numbers but never its value, on every id-like column, and no row dropped; an unknown multi-select token gets the three resolutions | nothing |
| `test_inputs.py` | A file over the size cap, a zip that declares far more than it holds, an XLSX that isn't a zip, an entity declaration in the workbook's XML, a cell over the length cap, a row over the width cap, a file over the row cap, XML cut short, a CSV in the wrong encoding and a missing file are each refused with a reason and a count and never the content (`THREAT_MODEL.md`, row 1); the cell and width caps default to Excel's own limits and apply to the definition workbook as well | nothing |
| `test_cost.py` | The estimate reproduces `docs/05` section 2 (2.83M tokens, £3.97 cached, £5.09 uncached per 5,000 open answers), the rates are settings, and the assumptions print with the number | nothing |
| `test_cli_validate.py` | `consult validate` prints the report and exits 0, 1 on an error, 2 on a refusal, and never quotes an open answer; a newline or an escape sequence in a cell can't forge a line of the report; `--json` prints the report as a document with the same counts, warnings and estimate | nothing |
| `test_stage.py` | The file is copied into one logged table per upload in the `staging` schema, named by the consultation id, every column text plus the file's row number; the consultation records the file's sha256 and row count and moves to staged; a header over 63 bytes and a repeated or blank header are refused before the edge and before any table, with a count and never a cell | Postgres |
| `test_configure.py` | The fixture's questions become rows with kind, response type, ordinal and the follow-up's related question; options are rows with the comma option merged back into one; `column_roles` names the id and ignored columns; `value_policy` carries the `N/A` decision and the `Unsure` mapping; a second save upserts and leaves the option list as it was; a column moved between sheets on re-save gets the status its new kind needs and an open question that has progressed keeps its state; two columns given the respondent-id role are refused before a write | Postgres |
| `test_ingest.py` | The long table against counts read from the CSV: one row per respondent per question, one per chosen option for the multi-select, a blank row for `-` and empty cells, open answers with their hash; `attrs` as `docs/04` section 5 describes and equal to what SQL rebuilds from the answer rows; the proforma flagged at answer and respondent level with nothing deleted; one pending `find_themes` job per open question on the consultation's run id, the consultation processing and the staging table dropped; four runs over one table leave the same rows and report zeros after the first, and a redelivery after the consultation has moved on does the same; a repeated respondent id refused before a row is written unless the resolution keeps the first or ignores the column, and a `-` in the id column is no id; ingest refuses a consultation in the wrong state, one with nothing configured (rather than writing empty respondents and dropping the only copy of the answers) and one whose staging table has gone; `N/A` treated as not answered when the policy says so, and an unknown value mapped to an existing option or added as a new one landing on it, all read off the CSV | Postgres |
| `test_vault.py` | Every email lands in `vault.respondent_identity` under the right department and nowhere else; `SET ROLE consult_pipeline` then a read or a write of the vault is refused at the schema | Postgres |
| `test_cli_ingest.py` | `consult ingest` takes the fixtures to a processing consultation another connection can see, prints counts and ids (the duplicate counts read back from the rows) and never a value from the file, and leaves the database empty after a definition error, a refused file or a refused ingest, which is rolled back before the connection's clean exit would commit it; a department name names one department across runs; the command installs the log formatter itself, so the `ingested` event reaches stderr through the console script | Postgres |

## What is proved, and what is not yet

Three of the four mechanics the design rests on (`docs/02`, section 13)
are proved here, each by a named test: the fan-in transaction
(`test_fan_in_race.py`), lease takeover with a fence (`test_jobs.py`) and
idempotent tag inserts (`test_tags.py`). The fourth, the indexed filter
query, is PR-09 (`plans/00-plan.md`). The vault refusal for the pipeline
role, promised in `docs/06` section 2.4, is `test_vault.py`. PR-04's
parsing and validator tests are all pure: `pytest -m 'not db'` runs every
one of them.
