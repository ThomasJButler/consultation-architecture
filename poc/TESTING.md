# Testing

What each test file proves, how the harness works, and how to run it. The
rule is CLAUDE.md rule 2: a behaviour gets a red commit that pins it before
the green commit that makes it pass. The settings test went in green with
the project it checks (plan step 1) and the repo rule with the documents
(step 12); every other test file here has a `Pin ...` commit ahead of the
code it holds.

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
| `test_config.py` | `.env.example` and `consult/config.py` name the same settings; the environment wins over `.env`; a missing password is named, not defaulted | nothing |
| `test_cli.py` | `consult init` creates the fourteen tables, the `vault` and `staging` schemas and the four roles from `docs/04` and `docs/06`, and running it twice is harmless; `--reset` drops and recreates the same schema with no rows surviving | Postgres |
| `test_fixtures.py` | The generator writes the three sheets with the headers in `docs/00`, comma-joined options including one containing a comma, one follow-up question with a placeholder, `-` and `N/A`; the committed fixtures are byte for byte what it writes | nothing |
| `test_fakes.py` | `RecordingLLM` keeps every prompt and answers well; `FakeLLM` answers with what it was told to, including each fault in `THREAT_MODEL.md` row 3, and raises when its script runs out | nothing |
| `test_store.py` | A failed job stores an error code and a provider request id; every column of `job` that can hold a string is on a named allow-list and `params` is held to a JSON object; the code vocabulary is a `CHECK` that names exactly the enum's values; a stale fence writes nothing | Postgres |
| `test_logs.py` | The formatter keeps ids, counts, durations, states and codes and drops everything else by name and by shape; a sentence as a message becomes a marker; an exception contributes its class and never its message | nothing |
| `test_repo_rules.py` | Every test module that needs a database is marked `db`, and only those | nothing |
| `test_definition.py` | The workbook parses into the three kinds of question in `docs/00`; the three response types are the vocabulary `docs/02` 3.2 names; `-` or blank means no related question; every problem is reported together and a bad response type doesn't cascade into a second problem | nothing |
| `test_responses.py` | CSV and XLSX read the same, one row at a time, with `-` and `N/A` kept as written; short rows are padded and long ones cut to the header | nothing |
| `test_tokenise.py` | A multi-select cell is matched by longest match against the vocabulary, never split on commas, and a token outside it is reported rather than guessed | nothing |
| `test_validate.py` | The whole report for the fixtures, with counts read from the CSV: no errors; the `Unsure` rows, the `N/A` column, the two options that never appear apart, the three unmatched headers with their default roles; a missing column is an error; a repeated respondent id is a warning with two resolutions and no row dropped | nothing |
| `test_inputs.py` | A file over the size cap, a zip that declares far more than it holds, an XLSX that isn't a zip, an entity declaration in the workbook's XML, a cell over the length cap and a file over the row cap are each refused with a reason and a count and never the content (`THREAT_MODEL.md`, row 1) | nothing |
| `test_cost.py` | The estimate reproduces `docs/05` section 2 (2.83M tokens, £3.97 cached, £5.09 uncached per 5,000 open answers), the rates are settings, and the assumptions print with the number | nothing |
| `test_cli_validate.py` | `consult validate` prints the report and exits 0, 1 on an error, 2 on a refusal, and never quotes an open answer; `--json` gives the same report as a document | nothing |

## What is not proved yet

The four mechanics the design rests on (`docs/02`, section 13) are not
tested here: the fan-in transaction, lease takeover with a fence, idempotent
tag inserts and the indexed filter query. The fan-in transaction, lease
takeover with a fence and idempotent tag inserts are PR-05; the indexed
filter query is PR-09 (`plans/00-plan.md`). The vault refusal for the
pipeline role is PR-06. PR-04 added parsing and the validator, all of it
pure: `pytest -m 'not db'` runs every one of those tests.
