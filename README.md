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
3. `poc/` is a small proof-of-concept of the parts of the design that are easy
   to claim and hard to get right: the job pipeline's claim, lease and fan-in
   mechanics, on a real Postgres. It runs offline with a fake model.
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

## Status (25 September 2026)

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
mechanics `docs/02` section 13 names proved by named tests. In review:
ingest (PR-06): the stage, configure and ingest steps as three functions
over one connection, `respondent.attrs` equal to what SQL rebuilds from
the answer rows, identity columns in the vault with the pipeline role
refused at the schema, both duplicate flags with nothing deleted, one
`find_themes` job per open question, a replay that writes nothing, and
`consult ingest` taking the fixtures to a processing consultation; the
minimum proof-of-concept now runs from a spreadsheet to a schema full of
rows with no model yet. Planned: themes and sign-off (PR-07), the worker
and reconciler (PR-08), queries and export (PR-09), final polish (PR-10).
Deliberately not built: see above.

## How it was built

Test-first, with the checks in CI: ruff, mypy, pytest against a real Postgres,
pip-audit and bandit (from PR-03 onwards). PR-01 to PR-03 were reviewed
before merging by [ReviewBot Protocol](https://github.com/ThomasJButler/ReviewBotProtocol),
a code-review tool I built and run myself: several open-weight models read the
diff independently on my own machine, their findings are compared, and nothing
leaves the device. From PR-04 the review pass is a Claude code review and a
Claude security review, run the same way: findings verified, fixed on the
branch or kept with a reason. What each review found, and what I did about
it, is in `docs/07-reviews.md`.
I used Claude throughout, to research, to stress-test alternatives and to
draft; the decisions are mine.

## Licence

MIT. See `LICENSE`.

## Built by

Thomas Butler.
