# PR-03: Proof-of-concept scaffold

**Status:** Planned   **Owner:** Thomas Butler   **Date:** 19 September 2026
**Depends on:** PR-02 (for `docs/04-data-model.md`)   **Branch:** `feat/03-poc-scaffold`

## 0. What this PR does and doesn't do

**Locks in:** the Python project under `poc/`, a Postgres 17 in Docker for
the tests, the schema from `docs/04`, the `consult init` command, the test
harness (one database per session, a truncating `db` fixture, `db` and
`slow` markers), the fakes the model tests will use, a fictional fixture
consultation in the real file format, CI on Python 3.12 with a Postgres
service container, pre-commit, and `TESTING.md`.
**Doesn't yet cover:** any parsing, ingest or pipeline logic. Those are
PR-04 onwards.

## 1. Objective

`docker compose up -d db && make check` is green on a fresh clone, and the
first commit already has CI running one real test, so every later failure
is isolated to the change that caused it.

## 2. Methodology

Build the smallest project that can run one database test, then add the
pieces in the order the later PRs will need them. psycopg 3 with row
factories rather than an ORM, because the SQL is the argument the design
makes. One `schema.sql` applied by `consult init` (with `--reset`), no
migration tool: a proof-of-concept changes its schema by rewriting the
file. The four roles (ingest, pipeline, export, `consult_admin`) are created in a `DO` block
guarded by a `pg_roles` lookup, because roles are cluster-wide and the tests
apply the schema to a fresh database each session; the vault grants are
per database and follow. The alternative, an ORM with migrations, was
rejected because it hides the exact statements the design rests on.

## 3. Test plan (defined first)

1. `test_env_example_names_every_setting` pins that `.env.example` and
   `config.py` agree.
2. `test_init_creates_every_table_the_design_names` pins that `consult
   init` creates the fourteen tables, the `vault` and `staging` schemas and
   the four roles in `docs/04`, and that running it twice is harmless.
3. `test_init_reset_leaves_the_same_empty_schema` pins that `--reset` drops
   and recreates everything and no rows survive.
4. `test_the_fixture_generator_writes_the_three_sheets_and_a_responses_file`
   pins the file format (sheet names, headers, `-` and `N/A`, one related
   closed question, one option containing a comma) with fictional content.
5. `test_the_fake_model_records_what_it_was_shown` pins `RecordingLLM`.
6. `test_the_scripted_fake_returns_what_it_was_told_to` pins the scriptable
   failure modes of `FakeLLM` (out-of-enum label, dropped id, extra id,
   prose, malformed JSON).
7. `test_pure_tests_run_without_a_database` is a repo rule: no test outside
   the `db` marker imports `psycopg` at module level.

## 4. Implementation steps

Each ends in a commit; subjects are plain sentences.

1. `Start the Python project with one green test and CI` (pyproject,
   Makefile, pytest.ini, test 1, the CI workflow; push and watch it pass
   before going on)
2. `Pin that init creates every table the design names` (test 2, red)
3. `Add the schema and the init command` (schema.sql, cli.py with `init`
   and `--reset`, store.py connection helpers; test 2 green)
4. `Bring up Postgres in Docker for the tests` (docker-compose bound to
   127.0.0.1 with a password from `.env`, conftest with the session
   database and the truncating `db` fixture, markers; test 3 red then green
   in two commits: `Pin that reset leaves the same empty schema`, `Make
   reset drop and recreate everything`)
5. `Pin what the fixture generator must produce` (test 4, red)
6. `Generate a fictional consultation in the real format` (the script and
   the committed fixtures; test 4 green)
7. `Pin the fakes the model tests will lean on` (tests 5 and 6, red)
8. `Add the recording and scripted fakes` (fakes.py; green)
9. `Run ruff, mypy and the guards before every commit` (pre-commit with
   mypy scoped to `consult/`, gitleaks, the brief guard)
10. `Say what each test file proves` (TESTING.md, poc/README.md with the
    "does not prove" list, test 7)
11. `Update the status block`

## 5. Output

| File | Purpose |
|---|---|
| `poc/pyproject.toml`, `Makefile`, `pytest.ini`, `.pre-commit-config.yaml`, `.env.example` | Project, gates, settings |
| `poc/docker-compose.yml` | Postgres 17 on loopback |
| `poc/consult/config.py`, `schema.sql`, `store.py`, `cli.py`, `llm.py` (protocol only), `logging.py` | The scaffold's own code |
| `poc/scripts/make_fixture_data.py`, `poc/tests/fixtures/` | Fictional consultation |
| `poc/tests/conftest.py`, `fakes.py`, `test_store.py`, `test_cli.py`, `test_fixtures.py`, `test_fakes.py`, `test_repo_rules.py` | The harness and the first tests |
| `.github/workflows/ci.yml` | Python job added to the guard job |
| `poc/README.md`, `poc/TESTING.md` | What it proves, what each file proves |

## 6. Security and quality notes

Postgres binds to loopback with a password from `.env`; `.env.example`
carries a placeholder. `mypy --strict` on `consult/` with `types-openpyxl`;
`ignore_missing_imports` is banned (CLAUDE.md rule 7). The fixture
generator must not reproduce anything from a real sample: different topic,
question texts, column references, option labels and vocabularies. Logging
policy from `THREAT_MODEL.md` is wired in `logging.py` and pinned in PR-04.

## 7. Fallback

If psycopg wheels are missing for the local Python, pin the project to
3.12 and run it in Docker too. If Docker isn't available, `pytest -m 'not
db'` still runs the pure tests and CI covers the rest.

## 8. Definition of done

- `docker compose up -d db && make check` green locally; CI green on the
  pull request head on Python 3.12.
- All seven tests in section 3 exist and pass.
- `README.md` Status block updated; `plans/PR-04-*.md` written; `RESUME.md`
  updated.
