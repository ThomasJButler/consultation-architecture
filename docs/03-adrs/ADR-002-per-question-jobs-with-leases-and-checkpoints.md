# ADR-002: Per-question jobs with leases, a fence and checkpoints

## Status

Accepted, 19 September 2026.

## Context

Tagging one open question with 100,000 answers is on the order of 10,000 model calls, and at the gateway's share of tokens per minute that is hours of wall clock (docs/05-scale-and-cost.md prints the arithmetic). The workers are Fargate tasks, which get replaced on every deploy, run out of memory, and lose the network. SQS standard queues deliver at least once, and "more than one copy of a message might be delivered" (SQS developer guide, standard queues, checked 19 September 2026). The unit of work therefore has to survive being killed halfway through and being handed to two workers at once, without paying the model twice for the same answers.

## Decision

The unit of work is one `job` row per (consultation, open question, kind, run), where kind is one of `stage`, `ingest`, `find_themes`, `preview_themes`, `map_themes`, `export`, `report`. The reconciler dispatches `pending` to `queued` under the caps, commits with `sent_at`, and only then sends a message carrying the job id and nothing else.

Claiming is one conditional update, and the number it returns is the fence:

```sql
UPDATE job
   SET status = 'running', attempts = attempts + 1,
       claimed_by = $worker, heartbeat_at = now()
 WHERE id = $job
   AND (status = 'queued'
        OR (status = 'running' AND heartbeat_at < now() - interval '10 minutes'))
RETURNING attempts;
```

Zero rows means one of three things (a live lease, already finished, unknown id); the worker logs which and deletes the message. Every later write the worker makes starts with:

```sql
UPDATE job SET heartbeat_at = now()
 WHERE id = $job AND claimed_by = $worker AND attempts = $fence AND status = 'running';
```

and aborts on zero rows. A background thread heartbeats the same way between writes. Progress is checkpointed per batch into `job_batch(job_id, batch_no, answer_ids, status, tokens, finished_at)` with primary key `(job_id, batch_no)` and `ON CONFLICT DO NOTHING`; a worker that takes over reads the last finished batch and resumes from the next. The results themselves are idempotent inserts (ADR-004), so a batch that was half-written is safe to redo.

The retry budget is `job.attempts < 5`, judged by the reconciler from the table. SQS's `maxReceiveCount` of 10 is a backstop, and a message in the dead-letter queue means "a message we didn't expect", never "the job failed". On SIGTERM a worker finishes its current batch and stops; batches are sized to finish inside `stopTimeout: 120`, which is the most Fargate allows (ECS task definition parameters, checked 19 September 2026).

Walk the failure. A worker runs out of memory at batch 3,000 of 5,000. Its heartbeat stops. Ten minutes later the reconciler re-sends (or a duplicate delivery of the original message arrives after the heartbeat has gone stale), `attempts` becomes 2, the new worker reads `job_batch` and starts at 3,000. One batch is paid for twice. Now the old container comes back (a paused process, a partition healing). It tries a write with fence 1, gets zero rows, and aborts. It can't spend and it can't corrupt. The term "fencing token" is Kleppmann's, from "How to do distributed locking" (2016); the mechanism is older than the name.

## Alternatives considered

**One job per consultation.** A smaller table, and the natural first draft. But a failure on question 7 of 70 blocks or loses the other 69, a retry re-pays everything, and sign-off per question (ADR-003) needs question-level state anyway.

**One job per batch.** Thousands of small messages, with retry granularity from the queue for free. The fan-in from batches to a question then needs the same predicate or counter as ADR-001, at 10,000 rows per question, and the per-consultation concurrency caps become hard to hold. The checkpoint table gives the same recovery from one message.

**The SQS visibility timeout as the lease, with no heartbeat or fence.** When the timeout lapses the message is redelivered and a second worker starts, but the first doesn't know it has lost the lease, and both write. Idempotent inserts make that harmless; the fence makes it free. The visibility timeout also tops out at twelve hours from first receipt, and extending it doesn't reset that limit (SQS developer guide, visibility timeout, checked 19 September 2026); a worker extending it is running a heartbeat by another name.

**Advisory locks held for the job.** `pg_advisory_lock` is released when the connection closes, which sounds right until a worker is alive but wedged and holds it indefinitely, and a worker that reconnects has nothing to check against. Heartbeat plus fence works across connections.

**A FIFO queue for exactly-once delivery.** The deduplication window is five minutes (SQS developer guide, message deduplication id, checked 19 September 2026); a re-send after a ten-minute stale lease falls outside it, and delivery guarantees say nothing about a worker dying mid-job, which is the case that matters.

## Consequences

- At-least-once delivery becomes recovery: only the interrupted batch is re-paid, and the per-job ledger (`tokens_in`, `tokens_cached`, `tokens_out`, `cost_pence`) stays honest across takeovers.
- Caps per department and per consultation are enforced at dispatch from the table, not from queue shape.
- Every write carries the `claimed_by` and `attempts` ritual, and the worker runs a heartbeat thread. Get either wrong and a job stalls silently. Both will be pinned by tests in the proof-of-concept (PR-05): lease takeover with a fence, and the exactly-once test.
- The ten-minute stale threshold is ten minutes of dead time before a takeover. The four-hour SLO for a 100,000-response consultation has room for it; a tighter threshold trades that for false takeovers.
- The heartbeat and the work share a process. A pause longer than ten minutes makes a live worker look dead, and the fence turns that into one wasted batch rather than a corrupted question. I'd rather waste a batch.

## How I'd know this was wrong

A job with `attempts` above 1 whose cost is close to double a sibling's, meaning a takeover re-ran more than one batch, or a tag row whose `job_id` and `batch_id` name a fence that had already been superseded.
