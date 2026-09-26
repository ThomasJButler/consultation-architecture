# CLAUDE.md

Working rules for this repository. Read `RESUME.md` first if it exists; it says
where the work is and which plan to open.

## What this repository is

A design for a production consultation-analysis service (docs, decision records,
a submitted three-page summary) and a proof-of-concept of its job-pipeline
mechanics on Postgres. See `README.md` and `plans/00-plan.md`.

## Key rules

1. **The brief stays out.** Never read, list or reference anything under
   `~/iai-brief-private/` from a session. Fixtures describe a fictional
   consultation; only the file format is shared. Enforced by
   `scripts/brief-guard.sh` and the local pre-commit hook.
2. **Tests first.** A behaviour gets a red commit that pins it (a test) before
   the green commit that makes it pass. A red test may import a
   name that does not exist yet; the green commit creates it. Pre-commit runs
   ruff and mypy on `poc/consult/` only, so red tests can be committed. CI must
   be green at the head of every pull request, not on every commit.
3. **Voice.** British English, contractions welcome, comments say why and cite
   the design (`docs/02`, an ADR), a Postgres or NCSC fact, or a number measured
   in this repo. No invented history: this repo has no past, so no "it used to
   be". No em dashes. No `TODO`, `FIXME` or `NOTE` markers; open work goes in
   the plan file.
4. **Commits.** The subject is a plain imperative sentence with no type prefix:
   "Drop the plain index the unique one replaces", not "chore: drop index".
   Capital first letter, 72 characters at most, no trailing full stop. A red
   test commit reads "Pin ..." and the green one that follows reads "Make ..."
   or names the change. A body only when it teaches something the diff does
   not. Merges use `--no-ff`. No history rewriting after
   a tag. **No `Claude-Session` trailer, no session link, no
   `Co-Authored-By: Claude`**, whatever a tool offers to append.
5. **Merge policy.** Nothing merges without the owner's review pass, docs and
   code alike, and the owner does the merging. A session opens the pull
   request, gets the checks green, fixes review findings on the branch, marks
   it ready, and stops. Branches are cut from `main`, never stacked.
6. **Every pull request ends the same way.** Update the dated Status block in
   `README.md`, write the plan for the next pull request in `plans/`, update
   `RESUME.md`, and stop.
7. **Types.** `mypy --strict` on `poc/consult/`. Never `ignore_missing_imports`;
   add typed stubs (`types-openpyxl`) instead. Use psycopg row factories so query
   results are typed without per-call asserts.
8. **Logs carry ids, counts, durations and error codes.** Never answer text.
   `job.error` stores an error code and a provider request id, never a message
   body. Pinned by a test.
9. **Model output is untrusted.** Labels are an enum of theme keys, every sent
   response id must come back exactly once, and the schema is validated in code.
   Responses go to the model as JSON-encoded data after a preamble that says
   instructions inside them are data.
10. **Postgres is the store.** SQL is parameterised, never formatted. The four
    mechanics the design rests on (the fan-in transaction, lease takeover with a
    fence, idempotent tag inserts, the indexed filter query) are proved by tests,
    not asserted.
11. **Numbers carry a source and a date.** Any figure in `docs/` names where it
    came from and when it was checked; anything unverified is rounded or left
    out.
12. **Nothing here names the recruitment process, the brief's authorship, or
    what the incumbent team has or has not built.** Prior art is cited as public
    documents and nothing more.

## Essential commands

```bash
# Proof-of-concept (from PR-03 onwards)
cd poc && docker compose up -d db     # Postgres 17 on 127.0.0.1
make check                            # ruff, mypy, pytest with coverage, pip-audit, bandit
pytest -m 'not db'                    # the pure-function tests, no Docker needed

# Guards
sh scripts/brief-guard.sh --all       # what CI runs
```

## Where to look first

| Question | File |
|---|---|
| What was submitted? | `SUBMISSION.md`, `submission/` |
| Why this shape? | `docs/02-architecture.md`, `docs/03-adrs/` |
| What does the data look like? | `docs/00-brief-and-data-shape.md`, `docs/04-data-model.md` |
| What does it cost? | `docs/05-scale-and-cost.md` |
| What is the next piece of work? | `RESUME.md`, then `plans/` |

## Status

26 September 2026: themes and sign-off in review (PR-07); PR-03 to PR-06 merged. See the Status block in `README.md`.
