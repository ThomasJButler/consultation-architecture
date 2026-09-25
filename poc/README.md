# Proof-of-concept

A small, runnable proof-of-concept of the parts of the design that are easy
to claim and hard to get right: the job pipeline's claim, lease and fan-in
mechanics on a real Postgres, with a fake model so it runs offline. The
design is `docs/02-architecture.md`; the schema is typed from
`docs/04-data-model.md`. This pull request (PR-03) lays the scaffold those
proofs stand on; the last section says what is proved so far, and
`TESTING.md` which pull request proves the rest. This is not the product,
and `../README.md` says what it deliberately leaves out.

## Run it

```bash
cp .env.example .env            # then change the password
docker compose up -d db         # Postgres 17, loopback only
make venv                       # Python 3.12; every dependency pinned
make init                       # consult init: apply schema.sql
make check                      # the gate CI runs
make validate                   # consult validate on the fixtures
```

`make reset` (`consult init --reset`) drops everything the schema creates
and applies it again. There is no migration tool: a proof-of-concept
changes its schema by rewriting `consult/schema.sql` and resetting.

## What is here (PR-03 and PR-04)

| Path | What it is |
|---|---|
| `consult/schema.sql` | Fourteen of the design's sixteen tables (`docs/04`, section 8 says which two stay out), the `vault` and `staging` schemas, the four roles and their grants |
| `consult/store.py` | Connections with dict rows, `init`, `reset`, `record_failure` |
| `consult/cli.py` | `consult init [--reset]`; `consult validate RESPONSES --definition WORKBOOK [--json]` |
| `consult/definition.py` | The definition workbook, read into typed questions (`docs/00`) |
| `consult/responses.py` | The responses file, CSV or XLSX, one row at a time |
| `consult/tokenise.py` | Multi-select cells matched by longest match against the option vocabulary, never split on commas |
| `consult/validate.py` | The validator from `docs/02` section 3.2: errors, warnings with resolutions, distinct values with counts |
| `consult/inputs.py` | The input guards from `THREAT_MODEL.md` row 1, with the caps as settings |
| `consult/cost.py` | The token and cost estimate from `docs/05`, with its assumptions printed |
| `consult/report.py` | The report rendered for a terminal or as JSON |
| `consult/config.py` | Settings from the environment, then `.env`; held to `.env.example` by a test |
| `consult/llm.py` | The model boundary: `Prompt`, `Completion`, the `LLM` protocol |
| `consult/logs.py` | The log formatter that lets through ids, counts, durations, states and codes and nothing else |
| `consult/errors.py` | The error code vocabulary `job.error_code` is held to |
| `scripts/make_fixture_data.py`, `tests/fixtures/` | A fictional consultation in the real file format |
| `tests/fakes.py` | `RecordingLLM` and `FakeLLM` |
| `TESTING.md` | What each test proves and how the harness works |

## The roles

`schema.sql` creates `consult_ingest`, `consult_pipeline`, `consult_export`
and `consult_admin` as `NOLOGIN` roles (`docs/06`, section 2.4 as
corrected). A connection is made as the login user in `.env` and `SET ROLE`
picks the grant set. The pipeline role has no grant on the `vault` schema
at all; only `consult_admin` holds `DELETE` anywhere. PR-06 will connect as
the pipeline role and expect the vault to refuse it.

## The fixtures

`tests/fixtures/definition.xlsx` and `responses.csv` describe a made-up
consultation on a riverside cycle route, the same fiction as the wireframes
in `docs/02`. Only the file format is shared with any real sample
(`docs/00`; `SECURITY.md`). The generator is seeded and the workbook's
timestamps are fixed, so `make fixtures` rewrites the same bytes and a test
holds the committed copies to it. The data carries the awkward cases on
purpose: an option containing a comma, a follow-up question with a
placeholder, `N/A`, a closed value outside its option list, a campaign
proforma repeated word for word, and one answer starting with `=`.

## What it does not prove

- Scale. The fixtures are 240 respondents; the plan benchmark at 20,000
  rows is PR-09, and ten million is a staging load test, not this.
- Any AWS wiring: no S3, no SQS, no Notify. The queue message is a hint in
  the design and the job table is the truth, so the mechanics can be proved
  without the queue.
- The web app or the dashboard. The proof-of-concept stops at a command
  line.
- Model quality. The model is a fake. What is proved is what happens to a
  reply that is wrong in each of the ways the threat model names.
- The four mechanics themselves. PR-03 laid the scaffold and PR-04 the
  parsing they will stand on; `TESTING.md` says which pull request proves
  each.

## Pre-commit

From the repository root, once: `poc/.venv/bin/pre-commit install`. It runs
ruff, mypy on `consult/` only (so a red test can still be committed),
gitleaks and the brief guard. An existing `.git/hooks/pre-commit` is kept
as `pre-commit.legacy` and runs it first, so the private half of the guard
in `SECURITY.md` carries on.
