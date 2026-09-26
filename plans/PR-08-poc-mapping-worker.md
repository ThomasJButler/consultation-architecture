# PR-08: Mapping, the worker loop and the reconciler

**Status:** Planned, revised 26 September 2026 against the merged PR-07
**Owner:** Thomas Butler   **Date:** 26 September 2026
**Depends on:** PR-07   **Branch:** `feat/08-poc-mapping-worker`

## 0. What this PR does and doesn't do

**Locks in:** the pipeline running without a hand on each job.

- **Dispatch under the caps** (docs/02, step 4): six jobs a department, four
  a consultation, twenty in all, round-robin when contended. It replaces the
  `jobs.queue` stand-in PR-07 left.
- **A worker loop** that claims whatever is queued, or whatever has a stale
  lease, and runs it by kind (`consult worker --once`).
- **The `map_themes` job** (step 9):
  - every distinct answer of a signed-off question, shuffled with a stored
    seed and sent in batches of ten against the frozen enum;
  - the two-way check;
  - a batch that fails is retried at size one, and an answer that still
    fails goes in the `unprocessable` bucket;
  - tags go in under the fence through `tags.insert_tags` and are copied to
    exact duplicates;
  - a checkpoint per batch, then fan-in 2 and the `analysis_ready` email.
- **Backoff on a gateway error** (section 9): full jitter, one to sixty
  seconds, six attempts, with the failure recorded as a code.
- **The failed edges docs/02 section 6 draws:** `find_failed` or
  `map_failed`, the consultation's `attention_reason`, and one
  `attention_needed` row.
- **The reconciler's statements** (section 5), as one command, each
  idempotent.

Batches are sized so no transaction comes near the ten-minute lease, since
`now()` is transaction start (PR-05's security review, `docs/07` row 05).

**Doesn't yet cover:**

- SQS, Notify and S3, which stay hints. The job table is the truth, and the
  relay marks rows sent with a fake reference.
- The filter query, exports and the plan benchmark (PR-09).
- Re-staging from a stored upload (there's no upload store).
- The web app's screens.
- **The `stage` and `ingest` job rows PR-06 deferred here.** Stage and ingest
  run inline in their commands and there's no upload store to re-run them
  from. A row with no runner would take a cap slot and hand the worker a job
  it can't do, so they stay out, and the four docstrings that promise them
  to this PR say so instead (`transitions.start_staging`,
  `ingest._queue_find_themes`, `jobs.queue` until it goes, `cli._run_job`).
- **`review_reminder` rows** (docs/02 correction 7, statement 5). The
  reminder is keyed on five working days in `themes_ready`, and neither
  `question` nor `theme_set_version` records when a question got there.
  That's a schema change for an email none of the four mechanics needs, so
  statement 5 relays and nothing more, and the plan says so.
- **ADR-006's worker fast-path relay.** ADR-006 has the worker relay right
  after the commit that wrote an outbox row, with the reconciler's statement
  5 as the slow path. `worker.run_once` doesn't relay, so every email waits
  for the next reconcile pass, up to five minutes; the reconciler's module
  docstring says so too.

## 1. Objective

- **The happy path.** After `consult ingest` on the fixtures, `consult
  worker --once` twice leaves both questions `themes_ready` and the
  consultation `awaiting_review` with one email row. Then `consult sign-off`
  on each and `consult worker --once` twice more leave:
  - both questions `complete`;
  - every distinct answer tagged once against v2, with its duplicates
    carrying the same tags;
  - the consultation `ready` with one `analysis_ready` row.

  `consult reconcile` afterwards changes nothing.
- **A bad batch.** A job whose fake replies with a fault at size ten, and
  again at size one for one answer, leaves one `unprocessable` batch row and
  nine tagged neighbours.
- **A gateway failure.** A job whose fake raises a gateway error six times
  is `failed_retryable` with `gateway_unavailable` and no message body.
- **A dead worker.** A worker that never heartbeats again is taken over.
  At five attempts the job is marked `failed`, with:
  - the question `map_failed`;
  - the consultation's `attention_reason` set to `map_failed:<question>`;
  - one `attention_needed` row whose subject is the job id.

## 2. Methodology

Four modules and a command each. The rest of this section is what the
first version of this plan got wrong or left out, read off the code on
`main` at `914936e`.

**Transitions first.** Nothing moves a question `signed_off →
assigning_themes`, yet `finish_map_themes` insists on `assigning_themes`
(transitions.py:226-237). Nothing writes `find_failed` or `map_failed`
either. test_repo_rules bans `UPDATE consultation` outside
`transitions.py`, so both go there:

- `start_map_themes(conn, question_id) -> bool`, the twin of
  `start_find_themes`: False on a takeover, refusal on anything else.
- `fail_job(conn, job_id) -> Advance`, used by the reconciler. Under
  `lock_consultation` it does the following in one transaction:
  - marks the job `failed`;
  - moves the question to `find_failed` from `configured` or
    `finding_themes`, or to `map_failed` from `signed_off` or
    `assigning_themes`. It takes both states because a crash before the
    worker's first commit leaves the question where it was;
  - sets `consultation.attention_reason` to `find_failed:<question>` or
    `map_failed:<question>` (docs/02 section 6; the column is on
    `consultation`, schema.sql:40, not on the job);
  - inserts an `attention_needed` row whose `subject_id` is the job id
    (docs/02 correction 3). `_outbox` takes a subject rather than always
    reading `run_id`;
  - runs `advance_consultation`, so a last question failing on the find
    side flips the consultation to `awaiting_review` (statement 4's case).

**`consult/dispatch.py`** is step 4 in two halves.

- A **pure `select`**: given the pending jobs, the live counts (queued plus
  running) per department, per consultation and in all, and the caps, it
  returns the ids to queue.
  - Oldest first with the id as tiebreak, since one ingest gives every job
    the same `now()`.
  - Round-robin across departments, ordered by their oldest pending job.
  - It never exceeds any of the three caps.
  - It's pure so the hard part is testable without Docker.
- `dispatch(conn, settings)` does the rest in the caller's transaction:
  1. Take `pg_advisory_xact_lock` on a constant, because the inserting
     transaction and the reconciler can dispatch at once and would both see
     the same free slots.
  2. Read the pending `find_themes` and `map_themes` jobs, the counts and
     the department caps.
  3. Call `select`.
  4. Run one `UPDATE … WHERE id = ANY(%s) AND status = 'pending'`. It sets
     `queued` and `sent_at`, plus `model_alias` and a `params.seed` **only
     where absent**, so a retried job rebuilds the plan its checkpoints
     were cut from.

  Postgres refuses `FOR UPDATE` alongside window functions, which is why
  ranking isn't done in SQL. The caps come from three places:

  - the department cap is `department.concurrent_jobs_cap`;
  - the consultation and service-wide caps are two new settings,
    `CONSULT_JOBS_PER_CONSULTATION=4` and `CONSULT_JOBS_IN_ALL=20`, in
    `config.SETTINGS` and `.env.example` (a test holds the two equal);
  - `CONSULT_MODEL_ALIAS=fake` is the alias dispatch stamps.

  Ingest and sign-off call `dispatch` in the transaction that inserted the
  job (the ingest role holds UPDATE on `job`). `run-job` and the
  test_cli_themes callers at lines 49, 247 and 278 move from `jobs.queue`
  to `dispatch`, and `jobs.queue` goes.

**`consult/mapping.py`** is step 9 as stage functions over one connection,
in the shape of `themes.py`:

1. `load_map_job` reads the question, the `signed_off` version and the
   alias and seed dispatch stamped.
2. Batches of ten over `themes.distinct_answers`, shuffled with the seed,
   with the related closed answer filled.
3. `map_themes_prompt` against v2's keys, then `parse_assignments`.
4. Tags go in through `tags.insert_tags` with the fence. Duplicates' tags
   are copied by `duplicate_of_answer_id` with the same job and batch.
5. A `ReplyError` at size ten is retried one answer at a time: each success
   is its own `done` batch, and each survivor an `unprocessable` batch
   naming it.
6. `transitions.finish_map_themes`.

**Resume is by coverage, not by batch number.** `job_batch` is keyed
`(job_id, batch_no)` and the retry at size one adds batches, so PR-07's
"skip every `batch_no` below `next_batch_no`" would skip or repeat answers.
Mapping resumes like this:

- the answers still to do are the plan's answers minus every id in this
  job's `job_batch.answer_ids`;
- they're rebatched in plan order;
- each checkpoint takes its number from `next_batch_no` when it's written.

**`consult/worker.py`** is the loop.

- **Pick:** `SELECT id … WHERE kind IN ('find_themes', 'map_themes') AND
  attempts < 5 AND (status = 'queued' OR (status = 'running' AND
  heartbeat_at < now() - stale)) ORDER BY created_at, id LIMIT 1 FOR UPDATE
  SKIP LOCKED`, then `jobs.claim` on that id (the first version's fallback,
  now the plan). A queued-only picker would never see the stale job the
  objective takes over.
- **Run:** the job runs under `as_role(PIPELINE_ROLE)` as `run-job` does,
  calling `start_find_themes` or `start_map_themes` first, with commits
  between batches.
- **Failures:**
  - a `ReplyError` is recorded as its code;
  - a `GatewayError` after the backoff is recorded as its code and request
    id;
  - a lease lost while recording comes out as a code (PR-07's three
    patterns, `raise … from None` included).
- **The backoff is outside the transaction.** Every model call and every
  backoff sleep happens with **no transaction open**, because `now()` is
  transaction start and a heartbeat stamped after a sleep would already be
  stale. The sleeper and the random source are injected, and the jitter
  carries the S311 and B311 markers.

**`consult/llm.py`** gains `GatewayError(code: ErrorCode, request_id: str |
None)`, using the vocabulary in `consult/errors.py`. `tests/fakes.py` can be
scripted to raise it.

**`consult/reconciler.py`** runs section 5's statements in order, each its
own transaction:

1. Dispatch.
2. Recover: stale `queued` (`sent_at` over ten minutes) or stale `running`.
   Below five attempts, re-stamp `sent_at` (the proof-of-concept's
   "re-send"); at five, `fail_job`.
3. Retry: a due `failed_retryable` below five attempts goes back to
   `pending`; at five, `fail_job`. That closes a hole in the design:
   statement 3 only retried `attempts < 5` and statement 2 only scanned
   `queued` and `running`, so a job failing on its fifth attempt sat in
   `failed_retryable` for good. docs/02 gets a dated correction for it.
4. Re-run both fan-ins for any `processing` or `awaiting_review`
   consultation, under the lock.
5. Relay: pending rows to `sending` under `FOR UPDATE SKIP LOCKED`, then
   to `sent` with a fake `notify_id` and `sent_at`.

The alternative of a real SQS client behind a flag was rejected: nothing
in the four mechanics needs it, and every test would need a queue.

## 3. Test plan (defined first)

Expected values come from the design, the fixtures' CSV or the fakes'
script, never from the code under test.

1. `test_select_round_robins_under_the_three_caps` (pure, no database)
   pins:
   - six a department, four a consultation, twenty in all;
   - oldest first with the id tiebreak;
   - round-robin across two contended departments;
   - nothing selected when every slot is taken.
2. `test_dispatch_queues_under_the_caps_and_keeps_a_seed` pins:
   - `sent_at`, the alias and a seed stamped;
   - a retried job keeping its seed;
   - a second run queuing nothing until a slot frees;
   - two dispatchers on two connections never going over a cap.
3. `test_the_map_and_failed_edges_move_the_question` pins:
   - `start_map_themes` both ways;
   - `fail_job` from a working and a pre-working state;
   - the consultation's `attention_reason`;
   - one `attention_needed` row with the job id as subject;
   - fan-in 1 flipping when the last unfinished question fails.
4. `test_mapping_batches_ten_shuffled_answers_and_tags_under_the_fence`
   pins:
   - batches of ten distinct answers against v2's keys, in the seed's order;
   - a tag per answer per key with `source = 'ai'`, the job and the batch;
   - a stale fence writing nothing.
5. `test_duplicates_are_themed_once_and_their_tags_copied` pins the
   proforma: one model call for the canonical answer, and every copy
   carrying the same tags.
6. `test_a_failed_batch_retries_at_size_one_and_buckets_the_answer` pins
   the fake failing at ten and again at one for one answer:
   - nine tagged;
   - one `unprocessable` batch row naming it;
   - the job still succeeding.
7. `test_mapping_resumes_by_coverage_after_a_takeover` pins a takeover
   after a retry at size one: no answer sent twice, none skipped.
8. `test_mapping_finishes_the_question_and_fan_in_two` pins:
   - `complete`;
   - the consultation `ready` with one `analysis_ready` row when the last
     question completes;
   - `map_failed` blocking it.
9. `test_a_gateway_error_backs_off_then_records_a_code` pins:
   - six attempts with full-jitter waits from the injected sleeper;
   - no transaction open during any call or sleep;
   - then `failed_retryable` with the code and the request id, and the
     message nowhere in a row or a log line.
10. `test_the_worker_claims_the_oldest_job_and_runs_it_by_kind` pins:
    - `--once` on a `find_themes` job and then a `map_themes` job;
    - a stale running job taken over;
    - a job at five attempts left alone;
    - two workers on two connections never claiming the same job (the
      pattern in test_fan_in_race.py).
11. `test_recover_resends_a_stale_job_and_fails_it_at_five_attempts` pins
    statement 2 both ways.
12. `test_retry_returns_a_due_failure_to_pending_and_fails_the_fifth` pins
    statement 3:
    - an undue failure stays;
    - a due one goes back to `pending`;
    - one at five attempts is failed.
13. `test_the_reconciler_reruns_the_fan_ins` pins statement 4 on section 5's
    case: the last unfinished question has just failed, and the
    consultation flips to `awaiting_review` with its email.
14. `test_the_relay_marks_outbox_rows_sent_once` pins statement 5 with two
    relays racing on two connections, and one send per row.
15. `test_the_worker_and_reconcile_commands_run_the_fixtures_to_ready`
    pins the objective through the commands, and that `consult reconcile`
    on a finished consultation changes no row.

## 4. Implementation steps

Each ends in a commit through the tom-commit-voice skill. Each `Pin ...`
commit adds exactly one failing test, run and seen failing for the right
reason before the commit. The commit after it makes that test pass and
nothing else.

1. `Pin how dispatch picks jobs under the three caps` (test 1)
2. `Pick pending jobs round-robin under the caps` (`dispatch.select`, the two settings)
3. `Pin that dispatch queues under the caps and keeps a seed` (test 2)
4. `Queue picked jobs under an advisory lock and retire the stand-in`
5. `Pin the map and failed edges of a question` (test 3)
6. `Add the map and failed edges to the transitions`
7. `Pin that mapping batches ten and tags under the fence` (test 4)
8. `Map a signed-off question in batches of ten` (`mapping.py`)
9. `Pin that duplicates are themed once and their tags copied` (test 5)
10. `Copy a canonical answer's tags to its duplicates`
11. `Pin the retry at size one and the unprocessable bucket` (test 6)
12. `Retry a failed batch one answer at a time`
13. `Pin that mapping resumes by coverage after a takeover` (test 7)
14. `Resume mapping from the answers no batch has covered`
15. `Pin that mapping finishes the question and runs fan-in 2` (test 8)
16. `Finish the map job in the fan-in transaction`
17. `Pin the backoff and the failure code` (test 9)
18. `Back off on a gateway error and record the code` (`llm.GatewayError`)
19. `Pin the worker loop` (test 10)
20. `Claim the oldest runnable job and run it by kind` (`worker.py`)
21. `Pin recovery and the failure at five attempts` (test 11)
22. `Recover stale jobs and fail them at five attempts` (`reconciler.py`)
23. `Pin the retry statement and the fifth failure` (test 12)
24. `Return a due failure to pending and fail the fifth`
25. `Pin that the reconciler re-runs the fan-ins` (test 13)
26. `Re-run both fan-ins for a consultation that stalled`
27. `Pin the relay` (test 14)
28. `Relay unsent outbox rows once`
29. `Pin the worker and reconcile commands` (test 15)
30. `Add the worker and reconcile commands`
31. `Correct statement 3 for a job failing on its fifth attempt` (docs/02,
    a new dated correction section, append-only)
32. `Say where the stage and ingest rows went` (the four docstrings)
33. `Say what the new test files prove` (`TESTING.md`)
34. `Update the status block and write the plan for PR-09`, or revise the
    draft if one is already on `main`

## 5. Output

| File | Purpose |
|---|---|
| `poc/consult/dispatch.py` | Step 4: the pure pick and the locked UPDATE |
| `poc/consult/mapping.py` | Step 9: batches of ten, the two-way check, the retry at one, tags copied to duplicates, resume by coverage |
| `poc/consult/worker.py` | The loop: pick, claim, run by kind, commit per batch, back off, record failures |
| `poc/consult/reconciler.py` | Section 5's statements |
| `poc/consult/transitions.py` | `start_map_themes`, `fail_job`, `_outbox` with a subject |
| `poc/consult/llm.py` | `GatewayError` |
| `poc/consult/config.py`, `poc/.env.example` | The two caps and the model alias |
| `poc/consult/cli.py` | `consult worker [--once]`, `consult reconcile`; `run-job` through dispatch |
| `poc/tests/fakes.py` | A scripted gateway error |
| `poc/tests/test_dispatch.py`, `test_mapping.py`, `test_worker.py`, `test_reconciler.py`, `test_cli_worker.py` | The tests above; test 3 joins `test_transitions.py` |

## 6. Security and quality notes

- `THREAT_MODEL.md` rows 2 and 3 carry over from PR-07 to the mapping call.
  The retry at size one and the `unprocessable` bucket now bound what one
  injected instruction can spoil (docs/02, step 9), and the theme list goes
  as data exactly as PR-07's review left it.
- Row 4, the queue, is answered by the conditional claim and the job table
  as the truth.
- Every failure the worker records is a code and a request id. The gateway
  test asserts the exception's message never reaches a row or a log line,
  and log fields pass `logs.py`'s allow-list (`request_id`, not a new
  name).
- Every tag insert carries the fence and the two joins PR-05's security
  review asked for.
- A reviewer should read:
  - `dispatch.select` against docs/02 step 4;
  - `mapping.py`'s retry and resume against step 9 and ADR-002;
  - `fail_job` against section 6's failed edges;
  - `reconciler.py` statement by statement against section 5 and the new
    correction.

## 7. Fallback

- If the pure pick fights the caps, queue one job at a time in a loop under
  the same advisory lock, re-reading the counts each time, and say so.
- If resume by coverage is awkward alongside `tags.insert_tags`, keep the
  batch plan fixed and give the retries at size one batch numbers above the
  plan's last (plan size plus position), and say so.
- If a test needs a sleep, the design is wrong: inject the clock.

## 8. Definition of done

- `make check` green locally and in CI.
- All fifteen tests in section 3 exist, were each seen red before their
  green commit, and pass.
- `consult ingest`, `consult worker --once` twice, two sign-offs, `consult
  worker --once` twice and `consult reconcile` leave the schema as
  section 1 describes.
- The pull request is reviewed and its row 08 is in `docs/07-reviews.md`
  before it merges.
- `README.md` Status block updated, `plans/PR-09-*.md` current, `RESUME.md`
  updated if it exists.
