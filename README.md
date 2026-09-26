# Consultation analysis: a production architecture

> A design for turning a piloted consultation-analysis tool into a service every
> government department can rely on, with the working notes, the decisions and a
> small proof-of-concept that sit behind a three-page submission.

Government has to consult the public before major policy changes. Someone then
reads every response, works out the themes, and tags each response by hand. An
LLM can do the reading and the first pass at the themes; the hard part is making
that trustworthy, cheap to run for every department, and easy to operate with a
small team. That is what this repository is about.

## What it does

1. `SUBMISSION.md` and `submission/` hold the two diagrams and the short
   explanation that were submitted. That is the deliverable.
2. `docs/` holds the reasoning: the brief in my own words, the research with
   sources, the architecture, the decision records, the data model, the cost
   model, the security and governance notes.
3. `poc/` is a small proof-of-concept of the parts of the design that are
   easy to claim and hard to get right: the fan-in transaction, lease
   takeover with a fence, idempotent tag inserts and the indexed filter
   query, on a real Postgres, each of the four proved by a named test
   rather than asserted (`poc/TESTING.md`). It runs offline with a fake
   model, a fixture consultation taken end to end from `consult ingest`
   through `consult worker`, `consult sign-off`, `consult query` and
   `consult export`.
4. `plans/` holds the plan for each pull request, written before the work.

## What it deliberately does not do

- It is not the product. There is no web app and no dashboard here; the design
  describes them and the proof-of-concept stops at a command line.
- It does not prove scale or any AWS wiring. Those are described and costed,
  not built.
- It does not include the brief, the sample data, or anything derived from them.

## Handling

The brief and the sample data for this task were received under a recruitment
process and are marked OFFICIAL. They are not in this repository and never will
be. Everything here is my own work or synthetic data in the same shape.

## Status (26 September 2026)

Built: the plan, the scaffold, the design documents (brief, research,
architecture, seven decision records, data model, cost model, security notes
and threat model), both diagrams and a first draft of the submission with a
rendered PDF, reconciled in PR-02b before the schema was typed from `docs/04`.
Merged: the proof-of-concept scaffold (PR-03), parsing and validation
(PR-04) and the store mechanics (PR-05): the Python project under `poc/`,
the schema typed from `docs/04` with its four roles, the test harness on a
real Postgres, a fictional fixture consultation, the model fakes, the
logging policy pinned by tests, the definition and responses readers, the
validator from `docs/02` section 3.2, the input guards from the threat
model, the cost estimate from `docs/05`, `consult validate`, the claim with
its fence, checkpoints, both fan-ins through one routine, the sign-off
guard, the idempotent tag insert and the reopen, with three of the four
mechanics `docs/02` section 13 names proved by named tests, and ingest
(PR-06): stage, configure and ingest as three functions over one
connection, identity columns in the vault with the pipeline role refused
at the schema, both duplicate flags, a replay that writes nothing, and
`consult ingest` taking the fixtures to a processing consultation, so the
minimum proof-of-concept runs from a spreadsheet to a schema full of rows.
Merged: themes and sign-off (PR-07): the prompt contract from `docs/06`
pinned as a test, model output held to the schema, the enum and the
two-way id check in code, the `find_themes` job stage by stage with a
checkpoint per batch and a proven takeover, condensation to a capped
shortlist with lineage, a preview that gives every theme a count and
quotes, the reviewer's edits under the version guard, and three commands
that run a job with the fake, list a question's themes and sign them
off.
Merged: mapping, the worker and the reconciler (PR-08): dispatch under
the three caps as a pure pick and one locked `UPDATE`, the `map_themes`
job in batches of ten against the frozen keys with the two-way check, a
refused batch retried one answer at a time and the survivor in the
`unprocessable` bucket, tags under the fence and copied to duplicates,
resume by coverage, a worker loop that picks the oldest runnable job with
`FOR UPDATE SKIP LOCKED` and runs it by kind with full-jitter backoff and
no transaction open across a call, the failed edges and the attention row,
the reconciler's five statements with the fifth-attempt fix recorded as a
dated correction to `docs/02`, and two commands, `consult worker --once`
and `consult reconcile`, that take the fixtures from ingest to `ready`
with a hand on no job.
Merged: the filter query, the export and the plan benchmark (PR-09): the
dashboard's filter grammar parsed to a typed value and refused by code,
the scope CTE from `docs/04` section 6 composed with `psycopg.sql` and
held to the caller's department, so a hostile value never reaches the
statement's text and another department's id finds no row, the theme
table with its denominator in one statement and the related closed
question's distribution checked against hand counts, the `other:`
semi-join and the duplicate toggle, the XLSX export as text cells with
the neutralising prefix and a manifest, read as `consult_export` under
one snapshot with the identity column back from the vault, a seeded
generator at any scale, and the fourth mechanic proved: at 20,000
respondents the planner takes the GIN index and probes the answer table
by its unique key, with what that corrected in `docs/04`, `docs/05` and
ADR-004 recorded as dated corrections. Two commands, `consult query` and
`consult export`.
In review: the submission, finished (PR-10): `TESTING.md` names a test for
each of the four mechanics, pinned by a test that reads it; every design
document carries a dated correction where the proof-of-concept found it
otherwise; `poc/README.md` says only what is still unproved; and the
evidence for every submission bullet is in the pull request for the
owner's rewrite, which happens by hand before the PDF is rebuilt and
`v1.0-submission` is tagged. Deliberately not built: see above.

## How it was built

Test-first, with the checks in CI: ruff, mypy, pytest against a real Postgres,
pip-audit and bandit (from PR-03 onwards; `poc/`'s own `make check` runs the
same gate). PR-01 to PR-03 were reviewed
before merging by [ReviewBot Protocol](https://github.com/ThomasJButler/ReviewBotProtocol),
a code-review tool I built and run myself: several open-weight models read the
diff independently on my own machine, their findings are compared, and nothing
leaves the device. From PR-04 the review pass is a Claude code review and a
Claude security review, run the same way: findings verified, fixed on the
branch or kept with a reason. What each review found, and what I did about
it, is in `docs/07-reviews.md`, one row per pull request, complete to
row 10, the last being the round on the pull request that finished the
repository.
I used Claude throughout, to research, to stress-test alternatives and to
draft; the decisions are mine.

## Licence

MIT. See `LICENSE`.

## Built by

Thomas Butler.
