# ADR-001: Orchestration in the database, not a workflow engine

## Status

Accepted, 19 September 2026.

## Context

A consultation fans out into one job per open question, waits days or weeks for a person to sign off each question's themes, fans out again to tag every answer, and reaches two consultation-level milestones on the way ("all themes ready", "all questions complete"). Each milestone earns exactly one email. The pipeline has to run for every department on one platform that a small team operates, and it has to be runnable on a laptop, because the mechanics that make it safe are the mechanics that are hardest to test anywhere else. Something has to hold the state of that workflow and decide when a milestone has been reached.

## Decision

Postgres holds every fact and every piece of pipeline state. The `job` table is the record of what is running; an SQS message is a hint that a job exists, sent only after the row is committed. There's no workflow engine and no counter. Milestone detection happens inside the transaction that completes the last question, behind a row lock on the consultation, and the email is a row written in the same commit (ADR-006):

```sql
BEGIN;
SELECT id FROM consultation WHERE id = $c FOR UPDATE;
UPDATE job SET heartbeat_at = now()
 WHERE id = $job AND claimed_by = $worker AND attempts = $fence AND status = 'running';
-- zero rows: ROLLBACK, the lease is gone

UPDATE question SET status = 'themes_ready'
 WHERE id = $q AND status = 'finding_themes';

WITH flipped AS (
  UPDATE consultation SET status = 'awaiting_review'
   WHERE id = $c AND status = 'processing'
     AND NOT EXISTS (SELECT 1 FROM question
                      WHERE consultation_id = $c AND kind = 'open'
                        AND status IN ('configured', 'finding_themes'))
  RETURNING id)
INSERT INTO notification_outbox (consultation_id, kind)
SELECT id, 'themes_ready' FROM flipped
ON CONFLICT DO NOTHING;
COMMIT;
```

docs/02-architecture.md writes the same thing as two statements guarded by the row count; the CTE is the single-statement form and either is fine. The proof-of-concept will use the two-statement one (PR-05).

The second fan-in (`ready`) has the same shape with `status <> 'complete'` as the not-yet-reached test. The predicate is a positive list of the states a question hasn't got past yet, so a question a reviewer has already signed off can't hold the email back.

Why the lock is its own statement, first. Under READ COMMITTED every statement takes a fresh snapshot, and a blocked `UPDATE` re-evaluates only its own `WHERE` clause against the new version of the row it's blocked on; the manual is explicit that such a command "does not see effects of those commands on other rows in the database" (PostgreSQL 17 manual, section 13.2.1, checked 19 September 2026). So if two questions finish together and the flip were the first statement, each finisher's `NOT EXISTS` would see the other's question still running, neither would flip, and the consultation would sit in `processing` for ever. That's the lost update. With `FOR UPDATE` taken first, the second finisher waits at the lock, and when it proceeds its next statement's snapshot includes the first finisher's commit. The double fire is handled by the other half of the guard: `status = 'processing'` is re-checked on the locked row, so at most one finisher flips, and the outbox's unique key (ADR-006) catches anything else.

The reconciler (docs/02-architecture.md, five statements, every five minutes) re-runs both predicates for any consultation whose questions have all arrived but whose status hasn't moved. Nothing depends on a worker being alive at the moment a milestone is reached.

## Alternatives considered

**AWS Step Functions.** The strongest alternative, and I'd want it on the record that it lost narrowly. Standard Workflows cost $0.025 per 1,000 state transitions with 4,000 free a month (AWS pricing page, US East figure, checked 19 September 2026); with one Task state per job and the batching kept inside the worker, a seventy-question consultation is a few hundred transitions, so money isn't the argument. An execution may run and idle for up to a year, a failed execution can be redriven within 14 days, and a Distributed Map can run up to 10,000 child executions (Step Functions quotas page, checked 19 September 2026). Visual history and redrive are genuinely useful when something's gone wrong at 2 a.m. Three things lost it. First, a running execution keeps the definition it started with (Step Functions API reference, UpdateStateMachine, checked 19 September 2026), so a consultation parked on a task token for a fortnight of review carries a two-week-old workflow through every deploy in between; with a row in Postgres, the next step runs whatever is deployed today. Second, the two milestones fall mid-branch, not at a Map's join: "all themes ready" fires while every branch is still open, waiting on sign-off, so the fan-in needs a counter or a query either way, and I'd rather it were the query I can show. Third, the 25,000-event history ceiling per execution (same quotas page) pushes a large consultation into child executions, at which point state lives in the execution history and in the database the dashboard reads, and those can disagree. Revisit if a second workflow shape appears (a scheduled re-analysis, a cross-consultation comparison), if a fan-out needs thousands of child executions, or if the platform team already runs Step Functions and wants one console for everything.

**A step-per-service pipeline: S3 as the interchange, EventBridge to trigger, Lambda to glue, Batch to run.** Consult's public ADR 0007 describes this shape and its reasons for moving to one queue and one worker with Postgres as the source of truth (checked 18 September 2026). State scattered across buckets and rules can't be run locally or proved by a test.

**Celery or RQ over Redis.** A second stateful service to run, and the fan-in primitive (a chord) keeps its count in the result backend, which is a second source of truth for the one thing that must never drift. RQ's dependencies are job-to-job and don't give the mid-branch milestone at all.

**Temporal.** Durable execution done properly, and the best fit for "park for two weeks". It's also a cluster to run or a service to pay for, and a programming model to learn, for one workflow shape with two milestones that a guarded `UPDATE` already handles. Same revisit condition as Step Functions.

## Consequences

- One store to back up and reason about. The state the reviewer sees on the dashboard is the state the pipeline acts on.
- The whole path will run against Docker Postgres on a laptop and in CI, and the fan-in race will be a named test (PR-05) rather than a diagram.
- I own the reconciler and the operator console (Django admin over `job`, `question`, `outbox`, `department`), which is what stands in for a workflow engine's execution history and redrive. It has to be built, and it's the kind of thing that gets built badly under deadline.
- Postgres is on the pipeline path as well as the read path. The worker writes per batch, never per answer, and the instance is sized from the first real consultation (docs/05-scale-and-cost.md).
- The consultation row lock serialises finishers. Seventy questions completing in the same second means seventy short waits, each for one transaction.

## How I'd know this was wrong

A consultation stuck between milestones that the reconciler's fourth statement doesn't unstick, or a second workflow shape in the backlog, and I'd move the per-question state machine to Step Functions and keep Postgres as the record.
