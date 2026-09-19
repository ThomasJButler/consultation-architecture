# Consultation analysis: a production architecture

> A design for turning a piloted consultation-analysis tool into a service every
> government department can rely on, with the working notes, the decisions and a
> small proof-of-concept that sit behind a two-page submission.

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

## Status (19 September 2026)

Built: the plan, the repository scaffold, the design documents (brief,
research, architecture, seven decision records), both diagrams and a first
draft of the submission with a rendered PDF. Planned: the data model, cost
model and security notes (PR-02), the proof-of-concept (PR-03 to PR-09),
final polish (PR-10). Deliberately not built: see above.

## Licence

MIT. See `LICENSE`.

## Built by

Thomas Butler.
