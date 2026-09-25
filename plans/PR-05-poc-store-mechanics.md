# PR-05: Store mechanics

**Status:** Planned   **Owner:** Thomas Butler   **Date:** 25 September 2026
**Depends on:** PR-04   **Branch:** `feat/05-poc-store-mechanics`

## 0. What this PR does and doesn't do

**Locks in:** the mechanics `docs/02` section 13 says will be proved by
tests rather than asserted, as functions over one connection with the SQL
visible: the claim with its fence (step 5), the heartbeat that every later
write starts with, the checkpoint per batch (ADR-002), fan-in 1 and fan-in
2 through one `advance_consultation` routine with the row lock first and
the outbox row in the same commit (step 7, step 10, ADR-001, ADR-006), the
sign-off guard that freezes v2 and inserts the `map_themes` job (step 8,
ADR-003), the idempotent tag insert that can't resurrect a retracted tag
(step 9, ADR-004), and the reopen that mints a new `run_id` (`docs/04`,
section 2). Plus the two tests ADR-001 promises: twenty threaded finishers
flipping a consultation exactly once, and the hand-stepped two-connection
reproduction of the lost update without the lock.
**Doesn't yet cover:** the worker loop, the reconciler and the outbox
relay (PR-08); ingest (PR-06); any model call (PR-07); the filter query
(PR-09). Rows are made by the tests' own factories.

## 1. Objective

`pytest -m db` proves three of the four mechanics on a real Postgres:
`test_the_fan_in_flips_exactly_once_under_twenty_threaded_finishers`,
`test_a_zombie_with_a_stale_fence_writes_nothing` and
`test_a_resumed_run_cannot_resurrect_a_retracted_tag` pass, and
`test_without_the_row_lock_two_finishers_lose_the_update` shows the
failure ADR-001 describes, so the fix is on record before the code that
needs it lands.

## 2. Methodology

One module per mechanic, each a few functions that take a connection and
never commit, so the worker (PR-08) composes them into the one transaction
the design describes: `consult/jobs.py` (claim, heartbeat, checkpoint,
`record_failure` moved from `store.py`), `consult/transitions.py`
(`advance_consultation`, `finish_find_themes`, `finish_map_themes`,
`sign_off`, `reopen_for_correction`), `consult/tags.py` (`insert_tags`,
`retract`, `restore`). Every write the worker makes goes through the
fence guard; a zero-row update raises `LeaseLost`, which `ErrorCode` already
names. Tests use `tests/rows.py`, extended with an open question, a theme
set version, a theme and an answer. The race test gives each of twenty
threads its own connection and a barrier, then counts status flips and
outbox rows. The reproduction without the lock runs the same fan-in on two
connections by hand, statement by statement, and asserts that neither
flips. The alternative, one big `worker.py` with the SQL inline, was
rejected because each mechanic then can't be tested without the loop
around it.

## 3. Test plan (defined first)

1. `test_claim_returns_the_fence_and_refuses_a_live_lease` pins step 5:
   a `queued` job is claimed and `attempts` comes back as the fence; a
   second claim on the live lease gets nothing.
2. `test_a_stale_lease_can_be_taken_over_and_the_fence_moves_on` pins the
   ten-minute threshold and that takeover increments `attempts`.
3. `test_a_zombie_with_a_stale_fence_writes_nothing` pins that heartbeat,
   checkpoint, tag insert and `record_failure` all refuse a stale fence
   and raise `LeaseLost`.
4. `test_checkpoints_are_idempotent_and_resume_from_the_last_batch` pins
   `job_batch` with `ON CONFLICT DO NOTHING` and `next_batch_no`.
5. `test_fan_in_one_flips_the_consultation_and_writes_one_outbox_row`
   pins step 7 end to end, with `subject_id` equal to the consultation's
   `run_id` and `awaiting_review_at` stamped.
6. `test_fan_in_one_waits_for_configured_and_finding_and_not_for_failed_or_signed_off`
   pins the positive-list predicate (a `find_failed` sibling doesn't block
   the email; a quick reviewer's `signed_off` doesn't either).
7. `test_fan_in_two_needs_every_open_question_complete` pins step 10 and
   that `map_failed` blocks it by design.
8. `test_the_fan_in_flips_exactly_once_under_twenty_threaded_finishers`
   (marked `slow`): twenty questions, twenty threads, one flip, one row.
9. `test_without_the_row_lock_two_finishers_lose_the_update` pins the
   READ COMMITTED behaviour ADR-001 cites, two connections hand-stepped.
10. `test_sign_off_is_a_guarded_update_that_admits_one_reviewer` pins
    ADR-003: the second reviewer gets nothing, v2 is frozen with `OTHER`
    and `NO_REASON`, one `map_themes` job exists under `job_one_per_run`.
11. `test_tag_inserts_are_idempotent_and_a_retracted_tag_stays_retracted`
    pins ADR-004's full unique index and the retract, restore, replay case.
12. `test_a_reopen_mints_a_run_id_so_the_second_email_has_its_own_row`
    pins `docs/04` section 2: a second `analysis_ready` row after a reopen.
13. `test_advance_consultation_is_the_only_writer_of_status` is a repo
    rule: no `UPDATE consultation SET status` outside `transitions.py`.

## 4. Implementation steps

Each ends in a commit; subjects are plain sentences.

1. `Pin the claim and the fence` (tests 1 to 3, red)
2. `Claim a job with a lease and refuse a stale fence` (`jobs.py`; green)
3. `Pin that checkpoints are idempotent and resumable` (test 4, red)
4. `Checkpoint each batch with ON CONFLICT DO NOTHING` (green)
5. `Pin the first fan-in` (tests 5 and 6, red)
6. `Flip the consultation behind the row lock and write the email`
   (`transitions.py` with `advance_consultation`; green)
7. `Pin the second fan-in` (test 7, red)
8. `Make the second fan-in wait for every question` (green)
9. `Pin that twenty finishers flip the consultation once` (test 8, red or
   already green; kept as the named proof either way)
10. `Show the lost update without the lock` (test 9, a test that asserts
    the failure)
11. `Pin the sign-off guard` (test 10, red)
12. `Freeze the signed-off version and queue the map job in one guard`
    (green)
13. `Pin the idempotent tag insert` (test 11, red)
14. `Insert tags on the full unique index and retract in place` (green)
15. `Pin the reopen's new run id` (test 12, red)
16. `Mint a run id on reopen` (green)
17. `Pin that nothing else writes consultation.status` (test 13)
18. `Say what the new test files prove` (`TESTING.md`, `poc/README.md`)
19. `Update the status block`

## 5. Output

| File | Purpose |
|---|---|
| `poc/consult/jobs.py` | Claim, heartbeat, checkpoint, `record_failure`, `LeaseLost` |
| `poc/consult/transitions.py` | `advance_consultation`, both fan-ins, sign-off, reopen |
| `poc/consult/tags.py` | Insert, retract, restore |
| `poc/tests/rows.py` | Factories for questions, versions, themes and answers |
| `poc/tests/test_jobs.py`, `test_transitions.py`, `test_fan_in_race.py`, `test_tags.py`, `test_repo_rules.py` | The tests above |

## 6. Security and quality notes

Every worker write is fenced, so the takeover case in ADR-002 is enforced
by the SQL and not by discipline. The outbox row is written in the
transition's commit and nowhere else. No answer text is logged: the log
lines carry job, question and consultation ids, batch numbers and counts
through `logs.log_event`. SQL stays parameterised. A reviewer should read
`advance_consultation` against `docs/02` step 7 and section 6, and the
tag insert against ADR-004's resurrection case.

## 7. Fallback

If twenty threads on one local Postgres are flaky in CI, drop to eight
and repeat the round three times; the property is one flip per round. If
the hand-stepped reproduction can't be made deterministic with two
connections, keep it as a documented script under `poc/scripts/` and cite
the manual's wording in the test that stays.

## 8. Definition of done

- `make check` green locally and in CI; the `slow` test runs in CI.
- All thirteen tests in section 3 exist and pass (test 9 passes by
  asserting the failure).
- `README.md` Status block updated; `plans/PR-06-*.md` written;
  `RESUME.md` updated.
