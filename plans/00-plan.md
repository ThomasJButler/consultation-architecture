# Plan

**Status:** Approved, 19 September 2026. **Owner:** Thomas Butler.

## 0. What this plan does and doesn't do

**Locks in:** the deliverable (two diagrams and a short explanation), the shape
of the design, the order of the pull requests, and what "done" means for each.
**Doesn't cover:** the wording of the submission itself, which is written by
hand at the end, and personal study notes, which are kept separately.

## 1. The deliverable

One document: two diagrams and ten to twelve bullets. Diagram 1 shows the
user's journey laid over the system. Diagram 2 shows one open question's life
through the pipeline, with the human sign-off as a hard state and the two
consultation-level transitions drawn, not footnoted. Everything else in this
repository is working material behind that document.

## 2. The design in a paragraph

The AI proposes themes and a named person signs them off per question; every
human edit is kept as evaluation data. Uploads are validated and costed before
any model spend, and demographics never enter a prompt. One boring platform
(one Postgres, one queue, one container image run as web, worker and a
five-minute reconciler, the team's existing model gateway, GOV.UK Notify) that
a small team can run for every department, with fairness enforced per
department. Underneath, every state transition is a database transaction: the
sign-off gate, the "all questions done" fan-in and the completion email are
one commit, so retries and duplicate deliveries cannot break them.

## 3. Sequence

| PR | Branch | Delivers |
|---|---|---|
| 00 | `main` | This scaffold: guards, README, licence, rules, plan, templates |
| 01 | `docs/01-design-freeze` | Brief in my words, research with sources, architecture, decision records, both diagrams, submission draft, first PDF |
| 02 | `docs/02-data-cost-security` | Data model, scale and cost, security and governance, threat model |
| 03 | `feat/03-poc-scaffold` | Python project, Docker Postgres, schema, CI, fixtures, first green test |
| 04 | `feat/04-poc-parsing` | Definition workbook and responses file parsing and validation |
| 05 | `feat/05-poc-store-mechanics` | Claim, lease and fence, checkpoints, both fan-ins, sign-off guard, tag insert, the exactly-once test |
| 06 | `feat/06-poc-ingest` | Ingest into the schema; the minimum proof-of-concept runs end to end |
| 07 | `feat/07-poc-themes-signoff` | Fake model, prompts, theme generation and the sign-off flow |
| 08 | `feat/08-poc-mapping-worker` | Theme mapping, the worker loop, the reconciler |
| 09 | `feat/09-poc-query-export-cli` | Filter queries, exports, the command line, the plan benchmark |
| 10 | `docs/10-submission-polish` | Final submission, review notes, how this was built |

Each pull request has its own plan in this folder, written when the previous one
merges. The proof-of-concept is only described anywhere once its minimum (PR-03
to PR-06) is merged and green.

## 4. Done criteria

- The submission document exists early (after PR-01) and is finalised last.
- `make check` is green on the head of every merged pull request.
- The four mechanics the design rests on are each proved by a named test.
- No file in the tree names the brief, its data, or its authorship.
