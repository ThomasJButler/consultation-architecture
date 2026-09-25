# Testing

What each test file proves, how the harness works, and how to run it. The
rule is CLAUDE.md rule 2: a behaviour gets a red commit that pins it before
the green commit that makes it pass, so every test here was red once.

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
| `test_store.py` | A failed job stores an error code and a provider request id, `job` has no text column a message body could go in, the code vocabulary is a `CHECK`, a stale fence writes nothing | Postgres |
| `test_logs.py` | The formatter keeps ids, counts, durations, states and codes and drops everything else by name and by shape; a sentence as a message becomes a marker; an exception contributes its class and never its message | nothing |
| `test_repo_rules.py` | Every test module that needs a database is marked `db`, and only those | nothing |

## What is not proved yet

The four mechanics the design rests on (`docs/02`, section 13) are not
tested here: the fan-in transaction, lease takeover with a fence, idempotent
tag inserts and the indexed filter query. They are PR-05 and PR-09. The
vault refusal for the pipeline role is PR-06. Parsing and the validator are
PR-04. This pull request proves the scaffold those will stand on and
nothing more.
