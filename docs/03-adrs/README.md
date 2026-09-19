# Decision records

Seven decisions the design rests on, each with the alternatives that lost and the consequences I'd rather have written down than discovered. They're derived from `docs/02-architecture.md`, which stays the master reference; if the two disagree, the architecture document wins and the record gets a correction.

| Record | The decision in a line | The mechanism it names |
|---|---|---|
| [ADR-001](ADR-001-orchestration-in-the-database.md) | Postgres holds every fact and every job; there's no workflow engine | A row lock, then a guarded `UPDATE` with a `NOT EXISTS` predicate, then the outbox insert, in one commit |
| [ADR-002](ADR-002-per-question-jobs-with-leases-and-checkpoints.md) | One job per open question, claimed with a lease and a fencing token, checkpointed per batch | A conditional `UPDATE ... RETURNING attempts`; `job_batch` with `ON CONFLICT DO NOTHING` |
| [ADR-003](ADR-003-human-sign-off-per-question.md) | A named person signs off the themes for each question before anything is tagged | `UPDATE question SET status = 'signed_off' WHERE status = 'themes_ready'` as the mutex |
| [ADR-004](ADR-004-data-model-long-table-and-immutable-versions.md) | A long answer table, a jsonb read model for filters, immutable theme-set versions, tags that are never deleted | GIN `jsonb_path_ops` on `respondent.attrs`; a full unique index on tags |
| [ADR-005](ADR-005-llm-access-through-the-gateway.md) | Every model call goes through the gateway with an alias pinned per consultation | Structured output plus an enum of theme keys and a two-way id check in code |
| [ADR-006](ADR-006-email-through-an-outbox.md) | The email is a row in the same commit as the state change that earns it | `UNIQUE (consultation_id, kind, subject_id)` on the outbox; `FOR UPDATE SKIP LOCKED` in the relay |
| [ADR-007](ADR-007-rollout-in-three-increments.md) | Three increments, parity with the manual script first | A replay of the piloted consultations as the acceptance gate |

## How to read them

Each record has the same six parts: Status, Context, Decision, Alternatives considered, Consequences, and a single line on how I'd know the decision was wrong. The last part is the one I'd check first in a year.

Facts carry their source and the date I checked them. Design choices (a ten-minute lease, five attempts, a semaphore of ten) are choices, and the record says why where there's a reason beyond taste.

Numbers about the piloted tool come only from the published evaluation report and the press release logged in `docs/01-research.md`. Where a public decision record from the Consult repository is cited, it's cited as a document.

## Amending a record

A record isn't edited in place once accepted. Add a `## Correction, <date>` section at the end saying what changed and why, and update the architecture document in the same pull request. A decision that's been reversed gets its Status changed to "Superseded by ADR-00N" and stays in the folder.
