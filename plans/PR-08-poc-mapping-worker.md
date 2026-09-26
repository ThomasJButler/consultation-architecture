# PR-08: Mapping, the worker loop and the reconciler

**Status:** Planned   **Owner:** Thomas Butler   **Date:** 26 September 2026
**Depends on:** PR-07   **Branch:** `feat/08-poc-mapping-worker`

## 0. What this PR does and doesn't do

**Locks in:** the pipeline running without a hand on each job. Dispatch
under the caps (docs/02, step 4: six jobs a department, four a
consultation, twenty in all, round-robin when contended), replacing the
`jobs.queue` stand-in PR-07 left; a worker loop that claims whatever is
queued and runs it by kind (`consult worker --once`); the `map_themes`
job (step 9): every answer of a signed-off question in batches of ten
against the frozen enum, the two-way check, a batch that fails retried at
size one and the answer that still fails put in the `unprocessable`
bucket, tags inserted under the fence through `tags.insert_tags` and
copied to exact duplicates, a checkpoint per batch, then fan-in 2 and the
`analysis_ready` email; backoff on a gateway error (section 9: full
jitter, one to sixty seconds, six attempts) with the failure recorded as a
code; and the reconciler's five statements (section 5) as one command,
each idempotent, with the `stage` and `ingest` job rows PR-06 deferred
inserted where dispatch inserts them. Batches are sized so no transaction
approaches the ten-minute lease, since `now()` is transaction start
(PR-05's security review, `docs/07` row 05).
**Doesn't yet cover:** SQS, Notify and S3, which stay hints (the job
table is the truth and the relay marks rows sent with a fake reference);
the filter query, exports and the plan benchmark (PR-09); re-staging from
a stored upload (no upload store); the web app's screens.

## 1. Objective

After `consult ingest` on the fixtures, `consult worker --once` twice
leaves both questions `themes_ready` and the consultation
`awaiting_review` with one email row; `consult sign-off` on each, then
`consult worker --once` twice more, leaves both questions `complete`, every
distinct answer tagged once against v2 with its duplicates carrying the
same tags, the consultation `ready` with one `analysis_ready` row, and
`consult reconcile` afterwards changing nothing. A job whose fake replies
with a fault at size ten and again at size one leaves one `unprocessable`
batch row and nine tagged neighbours. A job whose fake raises a gateway
error six times is `failed_retryable` with `gateway_unavailable` and no
message body, and a worker that never heartbeats again is taken over,
then marked `failed` at five attempts with the question `map_failed`, an
`attention_reason` and one `attention_needed` row.

## 2. Methodology

Four modules and a command each. `consult/dispatch.py` is step 4 as one
statement: the pending jobs a department and a consultation can still
take under the caps, oldest first, round-robin across departments, moved
to `queued` with `sent_at` in the caller's transaction; the caps are
settings. `consult/mapping.py` is step 9 as stage functions over one
connection in the shape of `themes.py`: batches of ten over the distinct
answers with the related closed answer filled, `map_themes_prompt`
against the signed-off version's enum, `parse_assignments`, tags through
`tags.insert_tags` with the fence, duplicates' tags copied by
`duplicate_of_answer_id`, a `ReplyError` at size ten retried one answer
at a time and the survivor's batch checkpointed `unprocessable`, then
`transitions.finish_map_themes`. `consult/worker.py` is the loop: claim
the oldest queued job (`FOR UPDATE SKIP LOCKED`), run it by kind with
commits between batches, record a `ReplyError` or a gateway failure as a
code with backoff for the latter, `--once` for the tests. `consult/
reconciler.py` is section 5's five statements in order, each its own
transaction. The gateway error is a `GatewayError` on `consult.llm`
carrying an `ErrorCode` and a request id, which the fakes can be
scripted to raise. The alternative of a real SQS client behind a flag was
rejected because nothing in the four mechanics needs it and every test
would need a queue.

## 3. Test plan (defined first)

1. `test_dispatch_queues_under_the_caps` pins six a department, four a
   consultation, twenty in all, oldest first, round-robin across two
   contended departments, `sent_at` stamped, and a second run queuing
   nothing more until a slot frees.
2. `test_mapping_batches_ten_and_tags_under_the_fence` pins batches of
   ten distinct answers against the frozen enum, a tag per answer per key
   with `source = 'ai'`, the job and batch on each, and a stale fence
   writing nothing.
3. `test_duplicates_are_themed_once_and_their_tags_copied` pins the
   proforma: one model call for the canonical answer, every copy carrying
   the same tags with the same job and batch.
4. `test_a_failed_batch_retries_at_size_one_and_buckets_the_answer` pins
   the fake failing at ten and again at one for one answer: nine tagged,
   one `unprocessable` batch row naming it, the job still succeeding.
5. `test_mapping_finishes_the_question_and_fan_in_two` pins `complete`,
   the consultation `ready` with one `analysis_ready` row when the last
   question completes, and `map_failed` blocking it.
6. `test_a_gateway_error_backs_off_then_records_a_code` pins six attempts
   with full-jitter waits (the sleeper injected), then `failed_retryable`
   with the code and the request id and no message anywhere.
7. `test_the_worker_claims_the_oldest_queued_job_and_runs_it_by_kind`
   pins `--once` on a find_themes and then a map_themes job, and two
   workers on one connection each never claiming the same job.
8. `test_recover_resends_a_stale_job_and_fails_it_at_five_attempts` pins
   statement 2 both ways, with `find_failed` or `map_failed`, the
   `attention_reason` and one `attention_needed` row on the job.
9. `test_retry_returns_a_due_failure_to_pending` pins statement 3 and that
   an undue one stays.
10. `test_the_reconciler_reruns_the_fan_ins` pins statement 4 on the case
    section 5 names: the last unfinished question just failed, and the
    consultation flips to `awaiting_review` with its email.
11. `test_the_relay_marks_outbox_rows_sent_once` pins statement 5 with
    two relays racing on two connections and one row each.
12. `test_the_worker_and_reconcile_commands_run_the_fixtures_to_ready`
    pins the objective through the commands, and that `consult reconcile`
    on a finished consultation changes no row.

## 4. Implementation steps

Each ends in a commit; subjects are plain sentences; one failing test per
`Pin ...`.

1. `Pin that dispatch queues under the caps` (test 1)
2. `Queue pending jobs under the caps, oldest first, round-robin`
   (`dispatch.py`; the `stage` and `ingest` rows; `jobs.queue` retired)
3. `Pin that mapping batches ten and tags under the fence` (test 2)
4. `Map a signed-off question in batches of ten` (`mapping.py`)
5. `Pin that duplicates are themed once and their tags copied` (test 3)
6. `Copy a canonical answer's tags to its duplicates`
7. `Pin the retry at size one and the unprocessable bucket` (test 4)
8. `Retry a failed batch one answer at a time`
9. `Pin that mapping finishes the question and runs fan-in 2` (test 5)
10. `Finish the map job in the fan-in transaction`
11. `Pin the backoff and the failure code` (test 6)
12. `Back off on a gateway error and record the code` (`llm.GatewayError`)
13. `Pin the worker loop` (test 7)
14. `Claim the oldest queued job and run it by kind` (`worker.py`)
15. `Pin recovery and the failure at five attempts` (test 8)
16. `Recover stale jobs and fail them at five attempts` (`reconciler.py`)
17. `Pin the retry statement` (test 9)
18. `Return a due failure to pending`
19. `Pin that the reconciler re-runs the fan-ins` (test 10)
20. `Re-run both fan-ins for a consultation that stalled`
21. `Pin the relay` (test 11)
22. `Relay unsent outbox rows once`
23. `Pin the worker and reconcile commands` (test 12)
24. `Add the worker and reconcile commands`
25. `Say what the new test files prove`
26. `Update the status block`

## 5. Output

| File | Purpose |
|---|---|
| `poc/consult/dispatch.py` | Step 4 under the caps |
| `poc/consult/mapping.py` | Step 9: batches of ten, the two-way check, the retry at one, tags copied to duplicates, fan-in 2 |
| `poc/consult/worker.py` | The loop: claim, run by kind, commit per batch, record failures |
| `poc/consult/reconciler.py` | Section 5's five statements |
| `poc/consult/llm.py` | `GatewayError` |
| `poc/consult/cli.py` | `consult worker [--once]`, `consult reconcile` |
| `poc/tests/fakes.py` | A scripted gateway error |
| `poc/tests/test_dispatch.py`, `test_mapping.py`, `test_worker.py`, `test_reconciler.py`, `test_cli_worker.py` | The tests above |

## 6. Security and quality notes

`THREAT_MODEL.md` rows 2 and 3 carry over from PR-07 to the mapping call,
now with the retry at size one and the `unprocessable` bucket that bound
what one injected instruction can spoil (docs/02, step 9). Row 4, the
queue, is answered by the conditional claim and the job table as the
truth. Every failure the worker records is a code and a request id; the
test for the gateway error asserts the exception's message never reaches
a row or a log line. Every tag insert carries the fence and the two
joins PR-05's security review asked for. A reviewer should read
`dispatch.py`'s statement against docs/02 step 4, `mapping.py`'s retry
against step 9, and `reconciler.py` statement by statement against
section 5.

## 7. Fallback

If round-robin across departments in one statement fights the caps, do
it as one statement per department in a loop over departments ordered by
how long they've waited, and say so. If the worker's `SKIP LOCKED` claim
is awkward alongside the fenced claim, pick the job id with `SKIP LOCKED`
and then call `jobs.claim` on it, which is what the reconciler's
re-send would do anyway.

## 8. Definition of done

- `make check` green locally and in CI.
- All twelve tests in section 3 exist and pass.
- `consult ingest`, `consult worker --once` twice, two sign-offs, `consult
  worker --once` twice and `consult reconcile` leave the schema as section
  1 describes.
- `README.md` Status block updated; `plans/PR-09-*.md` written;
  `RESUME.md` updated.
