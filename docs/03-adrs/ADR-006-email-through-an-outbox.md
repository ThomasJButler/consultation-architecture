# ADR-006: The email is a row in the same commit as the state change

## Status

Accepted, 19 September 2026.

## Context

The third step of the user's journey is to wait and get an email when the work is ready. Four milestone emails exist per consultation: themes ready, analysis ready, attention needed, and a review reminder. The state change that earns each one is a database commit (ADR-001), and the send is an HTTP call to GOV.UK Notify. Those two can't be made atomic. Send first and a rollback means an email about a state that never happened; commit first and a crash in between loses the email; retry the send and it goes twice. The same dual-write sits under the dispatch step, where a job row has to be committed and a queue message sent.

## Decision

`notification_outbox(id, consultation_id, kind, theme_set_version_id, subject_id, status pending | sending | sent, notify_id, created_at, sent_at)` with `UNIQUE NULLS NOT DISTINCT (consultation_id, kind, theme_set_version_id, subject_id)`. The row is inserted in the same transaction as the state transition, `ON CONFLICT DO NOTHING`. Milestone rows leave `subject_id` null, so one email per milestone per consultation is a constraint, not a convention; an attention row carries the failed job's id and a reminder row carries the question's id, so a second failure or a second reminder on the same consultation gets its own row instead of colliding on `(consultation_id, kind, NULL, NULL)` and being silently dropped. `NULLS NOT DISTINCT` matters because the milestone rows have no version id and no subject, and the nulls have to collide for the constraint to mean anything (PostgreSQL 17 manual, CREATE TABLE, checked 19 September 2026).

Two relays share one query. The worker tries the fast path immediately after its commit; the reconciler's fifth statement is the slow path every five minutes:

```sql
SELECT id, consultation_id, kind
  FROM notification_outbox
 WHERE status = 'pending'
 ORDER BY id
   FOR UPDATE SKIP LOCKED
 LIMIT 20;
```

`SKIP LOCKED` is the manual's own suggestion for "multiple consumers accessing a queue-like table" (PostgreSQL 17 manual, SELECT, the locking clause, checked 19 September 2026). The relay marks the row `sending`, calls Notify with `reference` set to the outbox id, then marks it `sent` with the Notify id. If a row is found in `sending` after a crash, the reconciler asks Notify for notifications by that reference before sending again; if one exists it records it and moves on.

The email carries the consultation's name, its status and a link. Never a response, a theme or a count. Notify is designed for messages classified OFFICIAL, including OFFICIAL-SENSITIVE, keeps message details for seven days by default, and stores and processes data in AWS data centres in the UK and Ireland (Notify security page, checked 19 September 2026). Sending email through it is free (Notify pricing page, checked 19 September 2026).

The guarantee is effectively-once with a stated at-least-once window: a crash after Notify has accepted the message and before `sent` is committed, where the reference lookup then also fails, could send the same email twice. That window is one HTTP round trip wide.

Dispatch to SQS has the same shape. The reconciler commits `queued` with `sent_at` first and sends second; a duplicate message is harmless because the claim is conditional (ADR-002), and a lost message is caught by the ten-minute re-send.

## Alternatives considered

**Call Notify inside the transaction.** A Notify timeout then holds the consultation's row lock for the length of the timeout, and a successful send followed by a rollback is an email about nothing. The textbook dual-write, and it lost first.

**Commit, then send, with no table.** Loses the email on a crash between the two, with nothing recording that it's owed. Recovering it needs the fan-in predicate plus a "did we already email" flag, which is the outbox with less structure.

**`LISTEN` and `NOTIFY` to a mailer process.** A Postgres notification is delivered on commit and dropped if nobody is listening. It's a wake-up, not a record. It could shave the fast path later; the row still has to exist.

**Amazon SES directly.** It works, and it's one fewer external service. Notify is the government's own channel, with templates, a sender domain the public recognises, an OFFICIAL assurance already written down, and no email cost. I'd need a reason not to use it, and I haven't got one.

**Notify's delivery callbacks to close the loop from `sent` to delivered.** On the cut list in docs/02-architecture.md. The reference lookup covers correctness; callbacks are a nicety for the operator console.

## Consequences

- One email per milestone is enforced by the database, so retries, duplicate queue deliveries and a reconciler running alongside a worker can't double-send.
- The email leaves within one reconciler cycle even with every worker dead, which is where the fifteen-minute SLO comes from.
- A row stuck in `sending` needs the reference lookup, an extra Notify call on a path that's easy to leave untested. It's in the runbook, and the proof-of-concept's fake Notify will record references so a test can exercise it.
- The at-least-once window is real, if narrow. A duplicate "themes ready" email is a nuisance and not a harm, which is why I've accepted it instead of adding a second table and a second protocol to be wrong about.

## How I'd know this was wrong

Two Notify notifications sharing one reference, or an outbox row older than fifteen minutes still `pending` while the reconciler's schedule shows it running.
