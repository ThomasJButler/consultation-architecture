# Proof-of-concept

A small, runnable proof-of-concept of the parts of the design that are easy
to claim and hard to get right: the job pipeline's claim, lease and fan-in
mechanics on a real Postgres, with a fake model so it runs offline. The
design is `docs/02-architecture.md`; the schema is typed from
`docs/04-data-model.md`. PR-03 laid the scaffold, PR-04 the parsing and
validation in front of it, PR-05 proves three of the four mechanics with
named tests, PR-06 takes a file into the schema end to end, PR-07 runs
the `find_themes` job stage by stage with the model a fake, through to a
signed-off theme set, PR-08 dispatches jobs under the caps, maps a
signed-off question in batches, and runs the worker loop and the
reconciler's five statements without a hand on each job, and PR-09 adds
the per-question filter query, the XLSX export and the plan benchmark
that proves the fourth mechanic; the section "What it does not prove"
says what is left, and `TESTING.md` which test proves it. This
is not the product, and `../README.md` says what it deliberately leaves
out.

## Run it

```bash
cp .env.example .env            # then change the password
docker compose up -d db         # Postgres 17, loopback only
make venv                       # Python 3.12; every dependency pinned
make init                       # consult init: apply schema.sql
make check                      # the gate CI runs
make validate                   # consult validate on the fixtures
make ingest                     # consult ingest on the fixtures: a consultation in processing
```

From there the review side runs by id, on `.venv/bin/consult` (plain
`consult` needs the venv on `PATH` instead). `consult ingest` prints the
consultation id; `worker --once`, `themes` and `sign-off` each run once
per open question, twice on the fixtures:

```bash
.venv/bin/consult worker --once                     # runs the oldest find_themes job
.venv/bin/consult worker --once                     # and the other one: awaiting_review
docker compose exec db psql -U consult -d consult -c \
  "select id, column_ref, status from question where kind = 'open'"
.venv/bin/consult themes <question id>               # once per open question
.venv/bin/consult sign-off <question id> --reviewer <uuid> --expect-version 0
.venv/bin/consult worker --once                     # runs the map_themes job sign-off queued
.venv/bin/consult worker --once                     # and the other one: ready
.venv/bin/consult reconcile                          # dispatch, recover, retry, fan-ins, relay
.venv/bin/consult query <question id> --filter attr:d_area=Villages --filter theme:<key>
.venv/bin/consult export <consultation id> --out out.xlsx
```

`consult run-job <job id> --worker w1 --model fake` runs one named
`find_themes` job by hand instead of a `worker --once` pick; `select id,
kind, status from job` in the same psql gives the job ids.

The model is `consult/fake_model.py`: it answers every prompt well, so
what these commands prove is the mechanics around the call and nothing
about theme quality.

`make reset` (`consult init --reset`) drops everything the schema creates
and applies it again. There is no migration tool: a proof-of-concept
changes its schema by rewriting `consult/schema.sql` and resetting.

## What is here (PR-03 to PR-09)

| Path | What it is |
|---|---|
| `consult/schema.sql` | Fourteen of the design's sixteen tables (`docs/04`, section 8 says which two stay out), the `vault` and `staging` schemas, the four roles and their grants |
| `consult/store.py` | Connections with dict rows, `init`, `reset`, `record_failure` |
| `consult/cli.py` | `consult init [--reset]`; `consult validate RESPONSES --definition WORKBOOK [--json]`; `consult ingest RESPONSES --definition WORKBOOK --name NAME --department NAME`; `consult run-job JOB --worker NAME --model fake`; `consult themes QUESTION`; `consult sign-off QUESTION --reviewer UUID --expect-version N`; `consult worker [--once] [--worker NAME] [--model fake] [--poll-seconds N]`; `consult reconcile`; `consult query QUESTION [--filter F]... [--department ID]`; `consult export CONSULTATION --out PATH` |
| `consult/definition.py` | The definition workbook, read into typed questions (`docs/00`) |
| `consult/responses.py` | The responses file, CSV or XLSX, one row at a time |
| `consult/tokenise.py` | Multi-select cells matched by longest match against the option vocabulary, never split on commas |
| `consult/validate.py` | The validator from `docs/02` section 3.2: errors, warnings with resolutions, distinct values with counts |
| `consult/inputs.py` | The input guards from `THREAT_MODEL.md` row 1, with the caps as settings |
| `consult/stage.py` | The file into one logged table per upload in the `staging` schema, by COPY, as the ingest role (`docs/02`, step 2; `docs/04`, section 2) |
| `consult/configure.py` | Questions, options, `column_roles` and `value_policy` from the definition, the report and the reviewer's resolutions (`docs/02`, step 3) |
| `consult/ingest.py` | The long answer table, `respondent.attrs`, the vault rows, both duplicate flags, the `find_themes` jobs and the processing edge, in one transaction that a redelivery repeats harmlessly (`docs/02`, step 3a; ADR-004) |
| `consult/dispatch.py` | The pure pick under the three caps and the locked `UPDATE` that queues it, guarded by an advisory lock so two dispatchers never over-fill a slot (`docs/02`, step 4) |
| `consult/prompts.py` | The prompt contract: the stable prefix, the data line, the answers as JSON-encoded data, the masks (`docs/06`, section 2.2) |
| `consult/replies.py` | Model output held to the schema, the enum and the two-way id check in code, a code and a reason on failure and never the text (CLAUDE.md, rule 9) |
| `consult/themes.py` | The `find_themes` job stage by stage with a checkpoint per batch: batches, generation, condensation, preview, then v1 and fan-in 1 (`docs/02`, steps 6 and 7; ADR-002) |
| `consult/mapping.py` | The `map_themes` job stage by stage: batches of ten against v2's frozen keys, the retry at size one and the `unprocessable` bucket, tags copied to duplicates, resume by coverage (`docs/02`, step 9; ADR-002) |
| `consult/review.py` | The reviewer's edits under the version guard, nothing deleted (`docs/02`, step 8; ADR-003) |
| `consult/fake_model.py` | The offline model: answers every prompt well and keeps what it was shown |
| `consult/cost.py` | The token and cost estimate from `docs/05`, with its assumptions printed |
| `consult/report.py` | The report rendered for a terminal or as JSON |
| `consult/jobs.py` | The claim with its fence, the heartbeat, the checkpoint, the failure record and the success mark, every write fenced (`docs/02`, step 5; ADR-002) |
| `consult/worker.py` | The loop: pick the oldest runnable job, claim it, run it by kind, back off on a gateway error with no transaction open (`docs/02`, step 5 and section 9) |
| `consult/transitions.py` | The only module that writes the consultation's status: `advance_consultation` for both fan-ins, the sign-off guard, the reopen, `start_map_themes` and `fail_job` (`docs/02`, steps 7, 8 and 10, section 6; ADR-001, ADR-003) |
| `consult/reconciler.py` | The five statements in order, each idempotent: dispatch, recover, retry, re-run both fan-ins, relay (`docs/02`, section 5) |
| `consult/tags.py` | Tags inserted on the full unique index, retracted in place, never deleted (ADR-004); a pair whose theme, answer and version don't line up writes nothing |
| `consult/query.py` | The filter grammar parsed to a typed value, the scope CTE composed with `psycopg.sql` and held to the caller's department, the theme table with its denominator, the related closed question's distribution (`docs/02`, step 11; `docs/04`, section 6; `docs/06`, section 2) |
| `consult/export.py` | The XLSX export: text cells, the neutralising prefix, the per-question summary and the manifest, the whole gather one REPEATABLE READ snapshot, read as `consult_export` (`docs/02`, step 12) |
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
at all; only `consult_admin` holds `DELETE` anywhere. `tests/test_vault.py`
connects as the pipeline role and the vault refuses it, at the schema, for
a read and for a write. The split guards against the code getting it
wrong, not against a compromised process: `SET ROLE` needs membership and
a member can `RESET ROLE`, and the login user here is the container's
superuser. A production worker's login user would be a member of
`consult_pipeline` and nothing else, so the boundary the test pins isn't
one `RESET ROLE` away (the security review of PR-06, `docs/07`).

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

- Scale past 20,000 rows. The plan benchmark proves the filter's shape
  there (`TESTING.md`); the 500 ms at ten million answers stays with the
  staging load test, not this (ADR-007).
- Any AWS wiring: no S3, no SQS, no Notify. The queue message is a hint in
  the design and the job table is the truth, so the mechanics can be proved
  without the queue.
- The web app or the dashboard. The proof-of-concept stops at a command
  line: no report as a print view or DOCX (`docs/02` step 12's second
  kind, cut for time since the XLSX carries the same numbers), no
  overview endpoint, no response cards or their page query, and no
  presigned links.
- Model quality. The model is a fake that answers every prompt well and
  condenses by key stem. What is proved is what reaches the model, what
  happens to a reply that is wrong in each of the ways the threat model
  names, and the mechanics around the call.
- A real model gateway, SQS or Notify call, and `review_reminder` rows.
  The fake stands in for the gateway throughout; the relay marks an
  outbox row sent with a fake reference; and nothing records when a
  question entered `themes_ready` to key a reminder on
  (`plans/PR-08-poc-mapping-worker.md`, section 0).
- The export's own department scope. `export.write_workbook` scopes by
  consultation id alone; the caller's department, which `query.scope`
  takes as a required keyword, isn't a parameter of the export yet
  (`docs/06`, section 2; `plans/PR-09-poc-query-export-cli.md`, section 7).
- The plan benchmark's node placement. The GIN scan feeding a Bitmap
  Heap Scan on `respondent`, with the answer key probed on a Nested
  Loop's inner side, was observed in PR-09's review round
  (`docs/07-reviews.md`, row 09) and isn't yet asserted by the test.
- The manifest's prompt hash and agreement rate (`docs/02`, step 12).
  `job.prompt_sha256` has its column and nothing here writes it; the
  agreement rate is derived from human edits and nothing here computes
  it; the manifest carries neither (`docs/02`'s correction of
  26 September 2026, item 3).
- The scaled fixture's duplicate text. At 20,000 rows 99.5% of the
  non-blank `o_reason` answers are exact duplicates, so the measured
  page is empty; the generator needs distinct open-answer text before
  the benchmark says anything about a consultation with few duplicates
  (`docs/05`'s correction of 26 September 2026, item 4).
- The GIN index's pending list after an ingest. `ANALYZE` doesn't flush
  it, so the first filter query after an ingest runs without the index
  until autovacuum reaches the table; a flush before the ingest commits,
  or `fastupdate = off`, is unmeasured (`docs/05`'s correction of
  26 September 2026, item 3).

## Pre-commit

From the repository root, once: `poc/.venv/bin/pre-commit install`. It runs
ruff, mypy on `consult/` only (so a red test can still be committed),
gitleaks and the brief guard. An existing `.git/hooks/pre-commit` is kept
as `pre-commit.legacy` and runs it first, so the private half of the guard
in `SECURITY.md` carries on.
