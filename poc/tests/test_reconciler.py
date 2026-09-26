"""What the reconciler's statements promise (docs/02, section 5).

Every five minutes, in order, each idempotent: a second pass over what
the first left changes nothing. Each test builds the rows a statement
scans by hand, runs it, and compares the rows read back with what the
section says it does. Expected values are the design's or a hand count,
never the code's.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow

from consult import reconciler, store
from consult.config import Settings
from consult.errors import ErrorCode
from consult.jobs import claim, heartbeat, record_failure
from tests.rows import (
    make_consultation,
    make_department,
    make_open_question,
    make_outbox_row,
    make_pending_job,
    make_queued_job,
)

pytestmark = pytest.mark.db

# Either side of the ten-minute lease (docs/02, step 5).
STALE = timedelta(minutes=11)
# A job is sent before it's claimed, so a lease that went quiet eleven
# minutes back was sent a little before that.
SENT_BEFORE_THE_CLAIM = timedelta(minutes=12)
# ADR-002: the retry budget is attempts < 5.
RETRY_BUDGET = 5
# Either side of now for next_attempt_at.
DUE = timedelta(minutes=-1)
NOT_YET_DUE = timedelta(minutes=1)
# ADR-006's relay takes twenty rows at a time. Thirty owed is more than
# one relay takes, so with two racing, both have to send.
RELAY_LIMIT = 20
OWED = 30
RELAYS = 2
# How long a pass may take before the test calls it stuck: the bound
# test_fan_in_race.py gives its barrier.
RETURNS_WITHIN_SECONDS = 30


def _jobs(db: psycopg.Connection[DictRow], consultation_id: UUID) -> dict[UUID, DictRow]:
    rows = db.execute("SELECT * FROM job WHERE consultation_id = %s", (consultation_id,))
    return {row["id"]: row for row in rows.fetchall()}


def _questions(db: psycopg.Connection[DictRow], consultation_id: UUID) -> dict[UUID, str]:
    rows = db.execute(
        "SELECT id, status FROM question WHERE consultation_id = %s", (consultation_id,)
    )
    return {row["id"]: str(row["status"]) for row in rows.fetchall()}


def _consultation(db: psycopg.Connection[DictRow], consultation_id: UUID) -> DictRow | None:
    return db.execute(
        "SELECT status, attention_reason FROM consultation WHERE id = %s", (consultation_id,)
    ).fetchone()


def _outbox(db: psycopg.Connection[DictRow], consultation_id: UUID) -> list[DictRow]:
    return db.execute(
        """
        SELECT kind, subject_id, status FROM notification_outbox
         WHERE consultation_id = %s ORDER BY id
        """,
        (consultation_id,),
    ).fetchall()


def _now(db: psycopg.Connection[DictRow]) -> datetime:
    """The database's clock, read in a transaction of its own and
    committed, so a stamp any later transaction writes is at or past it."""
    row = db.execute("SELECT now() AS now").fetchone()
    db.commit()
    assert row is not None
    stamp: datetime = row["now"]
    return stamp


def _age(
    db: psycopg.Connection[DictRow],
    job_id: UUID,
    *,
    attempts: int,
    sent_ago: timedelta,
    heartbeat_ago: timedelta | None = None,
) -> None:
    """Hand-set a job's attempts and its clocks, the heartbeat only when given."""
    db.execute(
        """
        UPDATE job SET attempts = %s, sent_at = now() - %s,
               heartbeat_at = coalesce(now() - %s::interval, heartbeat_at)
         WHERE id = %s
        """,
        (attempts, sent_ago, heartbeat_ago, job_id),
    )


def _failed(
    db: psycopg.Connection[DictRow],
    consultation_id: UUID,
    question_id: UUID,
    *,
    kind: str = "find_themes",
    attempts: int,
    retry_in: timedelta,
) -> UUID:
    """A job as a worker's failure leaves it: its attempt spent, the code
    and request id on the row, a time to retry (jobs.record_failure), and
    the alias and seed its first dispatch stamped (ADR-002)."""
    job_id = make_pending_job(
        db, consultation_id, question_id, kind=kind, model_alias="fake-earlier", seed=424242
    )
    db.execute(
        "UPDATE job SET status = 'queued', sent_at = now(), attempts = %s WHERE id = %s",
        (attempts - 1, job_id),
    )
    lease = claim(db, job_id, "w-failed")
    assert lease is not None
    assert lease.fence == attempts
    record_failure(
        db,
        lease,
        ErrorCode.GATEWAY_UNAVAILABLE,
        provider_request_id=f"req-{attempts}",
        retry_in=retry_in,
    )
    return job_id


def test_recover_resends_a_stale_job_and_fails_it_at_five_attempts(
    db: psycopg.Connection[DictRow],
) -> None:
    # Statement 2 (docs/02, section 5): a queued job whose send is over
    # ten minutes old, or a running one whose heartbeat is, is re-sent
    # below five attempts and failed at five. Two questions stay
    # configured, so fan-in 1 stays shut and the failure moves nothing
    # but its own edge.
    consultation_id = make_consultation(db, make_department(db), status="processing")
    q_stale_queued = make_open_question(db, consultation_id, "o_1", ordinal=1, status="configured")
    q_stale_running = make_open_question(db, consultation_id, "o_2", ordinal=2)
    q_spent = make_open_question(db, consultation_id, "o_3", ordinal=3, status="assigning_themes")
    q_fresh_queued = make_open_question(db, consultation_id, "o_4", ordinal=4, status="configured")
    q_live = make_open_question(db, consultation_id, "o_5", ordinal=5)

    stale_queued = make_queued_job(db, consultation_id, q_stale_queued)
    _age(db, stale_queued, attempts=1, sent_ago=STALE)

    stale_running = make_queued_job(db, consultation_id, q_stale_running)
    assert claim(db, stale_running, "w-dead") is not None
    _age(db, stale_running, attempts=2, sent_ago=SENT_BEFORE_THE_CLAIM, heartbeat_ago=STALE)

    spent = make_queued_job(db, consultation_id, q_spent, kind="map_themes")
    assert claim(db, spent, "w-dead") is not None
    _age(db, spent, attempts=RETRY_BUDGET, sent_ago=SENT_BEFORE_THE_CLAIM, heartbeat_ago=STALE)

    fresh_queued = make_queued_job(db, consultation_id, q_fresh_queued)
    live = make_queued_job(db, consultation_id, q_live)
    assert claim(db, live, "w-alive") is not None
    db.commit()

    before = _jobs(db, consultation_id)
    questions_before = _questions(db, consultation_id)
    started = _now(db)

    assert reconciler.recover(db) == (2, 1)

    after = _jobs(db, consultation_id)
    # Re-sent: a fresh sent_at and nothing else. The message is a hint and
    # the job table the truth (ADR-006, last paragraph), and nothing here
    # claims, so the running one keeps its stale lease for a worker's
    # conditional claim to take over (docs/02, step 5).
    for job_id in (stale_queued, stale_running):
        resent = after[job_id]
        assert resent["sent_at"] >= started
        assert resent == {**before[job_id], "sent_at": resent["sent_at"]}
    # A fresh send and a live lease are left as they were.
    for job_id in (fresh_queued, live):
        assert after[job_id] == before[job_id]
    # At five attempts, fail_job: the job failed, its question on the
    # failed edge, the consultation's attention_reason naming both, and one
    # attention_needed row whose subject is the job (docs/02, section 6
    # and correction 3). The consultation's status doesn't move.
    assert after[spent] == {**before[spent], "status": "failed"}
    assert _questions(db, consultation_id) == {**questions_before, q_spent: "map_failed"}
    assert _consultation(db, consultation_id) == {
        "status": "processing",
        "attention_reason": f"map_failed:{q_spent}",
    }
    outbox = _outbox(db, consultation_id)
    assert outbox == [{"kind": "attention_needed", "subject_id": spent, "status": "pending"}]

    # A second pass finds nothing to do. The running job's heartbeat is
    # still stale, but its re-send opened a ten-minute window of its own,
    # as a queued job's send does (ADR-006: "a lost message is caught by
    # the ten-minute re-send"); re-sending it on every pass would make the
    # statement anything but idempotent (docs/02, section 5).
    assert reconciler.recover(db) == (0, 0)
    assert _jobs(db, consultation_id) == after
    assert _outbox(db, consultation_id) == outbox


def test_recover_passes_over_a_job_row_a_paused_worker_holds(
    db: psycopg.Connection[DictRow], db_settings: Settings
) -> None:
    # Two jobs whose leases went quiet eleven minutes back, as far as any
    # other transaction can see. One worker has woken inside its batch
    # transaction: its fenced heartbeat holds that job's row lock and it
    # hasn't committed (a pause, a partition). The lock says the lease is
    # live, so statement 2 leaves the row for a later pass, as a worker's
    # pick SKIP LOCKs past it, and re-sends the other (docs/02, section 5
    # and step 5). Waiting on it would stall every statement after this one
    # for every consultation for as long as the worker stays paused.
    consultation_id = make_consultation(db, make_department(db), status="processing")
    q_held = make_open_question(db, consultation_id, "o_1", ordinal=1)
    q_quiet = make_open_question(db, consultation_id, "o_2", ordinal=2)
    held = make_queued_job(db, consultation_id, q_held)
    paused = claim(db, held, "w-paused")
    assert paused is not None
    quiet = make_queued_job(db, consultation_id, q_quiet)
    assert claim(db, quiet, "w-dead") is not None
    for job_id in (held, quiet):
        _age(db, job_id, attempts=1, sent_ago=SENT_BEFORE_THE_CLAIM, heartbeat_ago=STALE)
    db.commit()
    before = _jobs(db, consultation_id)
    started = _now(db)

    passes: list[tuple[int, int]] = []
    first = threading.Thread(target=lambda: passes.append(reconciler.recover(db)))
    with store.connect(db_settings) as holder:
        heartbeat(holder, paused)
        first.start()
        first.join(timeout=RETURNS_WITHIN_SECONDS)
        returned = not first.is_alive()
        # The worker's transaction ends without a commit, as a crash ends
        # it, which also lets a pass stuck behind the lock finish, so the
        # test fails on the assertion below rather than hanging.
        holder.rollback()
    first.join(timeout=RETURNS_WITHIN_SECONDS)

    assert returned
    assert passes == [(1, 0)]
    after = _jobs(db, consultation_id)
    assert after[held] == before[held]
    assert after[quiet]["sent_at"] >= started
    assert after[quiet] == {**before[quiet], "sent_at": after[quiet]["sent_at"]}

    # With the worker gone, the next pass re-sends the held job, and only it:
    # the other's re-send opened a ten-minute window of its own.
    assert reconciler.recover(db) == (1, 0)
    again = _jobs(db, consultation_id)
    assert again[held] == {**before[held], "sent_at": again[held]["sent_at"]}
    assert again[held]["sent_at"] >= started
    assert again[quiet] == after[quiet]


def test_retry_returns_a_due_failure_to_pending_and_fails_the_fifth(
    db: psycopg.Connection[DictRow],
) -> None:
    # Statement 3 (docs/02, section 5), and the hole the plan closes
    # (section 2): the section retried a due failure only below five
    # attempts and statement 2 scans only queued and running, so a job
    # failing on its fifth attempt sat in failed_retryable for good. Two
    # questions are still finding, so fan-in 1 stays shut.
    consultation_id = make_consultation(db, make_department(db), status="processing")
    q_undue = make_open_question(db, consultation_id, "o_1", ordinal=1)
    q_due = make_open_question(db, consultation_id, "o_2", ordinal=2)
    q_fifth = make_open_question(db, consultation_id, "o_3", ordinal=3, status="assigning_themes")
    undue = _failed(db, consultation_id, q_undue, attempts=1, retry_in=NOT_YET_DUE)
    due = _failed(db, consultation_id, q_due, attempts=3, retry_in=DUE)
    fifth = _failed(
        db, consultation_id, q_fifth, kind="map_themes", attempts=RETRY_BUDGET, retry_in=DUE
    )
    db.commit()

    before = _jobs(db, consultation_id)
    questions_before = _questions(db, consultation_id)

    assert reconciler.retry(db) == (1, 1)

    after = _jobs(db, consultation_id)
    # Not due yet: left for a later pass.
    assert after[undue] == before[undue]
    # Due below five: back to pending, and only the status moves. attempts,
    # params and model_alias stay, so the next dispatch keeps the seed and
    # the alias and the plan the job's checkpoints were cut from can be
    # rebuilt (ADR-002; dispatch stamps both only where absent). error_code
    # stays too. The plan doesn't say; keeping the last code until the next
    # failure overwrites it is the simpler reading, and the row still says
    # why the job went back.
    assert after[due] == {**before[due], "status": "pending"}
    # Due at five: failed through fail_job, with its question on the failed
    # edge, attention_reason and one attention_needed row (docs/02, section
    # 6 and correction 3).
    assert after[fifth] == {**before[fifth], "status": "failed"}
    assert _questions(db, consultation_id) == {**questions_before, q_fifth: "map_failed"}
    assert _consultation(db, consultation_id) == {
        "status": "processing",
        "attention_reason": f"map_failed:{q_fifth}",
    }
    outbox = _outbox(db, consultation_id)
    assert outbox == [{"kind": "attention_needed", "subject_id": fifth, "status": "pending"}]

    # A second pass finds nothing: the retried job is pending and the fifth
    # failed, and neither is failed_retryable any more.
    assert reconciler.retry(db) == (0, 0)
    assert _jobs(db, consultation_id) == after
    assert _outbox(db, consultation_id) == outbox


def _consultations(db: psycopg.Connection[DictRow]) -> dict[UUID, DictRow]:
    return {row["id"]: row for row in db.execute("SELECT * FROM consultation").fetchall()}


def _all_outbox(db: psycopg.Connection[DictRow]) -> list[DictRow]:
    # By kind, not by id: a pass takes consultations in whatever order it
    # likes, and their ids are random, so the rows' order is too.
    return db.execute(
        "SELECT consultation_id, kind, subject_id, status FROM notification_outbox ORDER BY kind, id"
    ).fetchall()


def test_the_reconciler_reruns_the_fan_ins(db: psycopg.Connection[DictRow]) -> None:
    department_id = make_department(db)
    # Statement 4's case (docs/02, section 5): the last unfinished question
    # has just gone to find_failed, no worker transaction runs to flip the
    # consultation, and fan-in 1 doesn't wait on a failed question. Set by
    # hand, the way the lost update in test_fan_in_race.py leaves a
    # consultation processing with every question past finding.
    stalled = make_consultation(db, department_id, "Stalled", status="processing")
    make_open_question(db, stalled, "o_1", ordinal=1, status="themes_ready")
    make_open_question(db, stalled, "o_2", ordinal=2, status="find_failed")
    # Every question complete and the consultation still awaiting review:
    # fan-in 2's half of the same miss.
    finished = make_consultation(db, department_id, "Finished", status="awaiting_review")
    make_open_question(db, finished, "o_1", ordinal=1, status="complete")
    make_open_question(db, finished, "o_2", ordinal=2, status="complete")
    # A failed mapping blocks ready by design (docs/02, step 10).
    blocked = make_consultation(db, department_id, "Blocked", status="awaiting_review")
    make_open_question(db, blocked, "o_1", ordinal=1, status="complete")
    make_open_question(db, blocked, "o_2", ordinal=2, status="map_failed")
    # Neither fan-in has anything to say to a consultation that's ready or
    # still a draft.
    ready = make_consultation(db, department_id, "Ready", status="ready")
    make_open_question(db, ready, "o_1", ordinal=1, status="complete")
    draft = make_consultation(db, department_id, "Draft", status="draft")
    make_open_question(db, draft, "o_1", ordinal=1, status="configured")
    db.commit()
    before = _consultations(db)

    assert reconciler.rerun_fan_ins(db) == 2

    after = _consultations(db)
    assert after[stalled]["status"] == "awaiting_review"
    assert after[finished]["status"] == "ready"
    for untouched in (blocked, ready, draft):
        assert after[untouched] == before[untouched]
    # Each flip's email is a row in the flip's own commit, naming the pass
    # (ADR-006; docs/02, correction 2).
    outbox = _all_outbox(db)
    assert outbox == [
        {
            "consultation_id": finished,
            "kind": "analysis_ready",
            "subject_id": before[finished]["run_id"],
            "status": "pending",
        },
        {
            "consultation_id": stalled,
            "kind": "themes_ready",
            "subject_id": before[stalled]["run_id"],
            "status": "pending",
        },
    ]

    # A second pass moves nothing and adds no row.
    assert reconciler.rerun_fan_ins(db) == 0
    assert _consultations(db) == after
    assert _all_outbox(db) == outbox


def test_the_relay_marks_outbox_rows_sent_once(
    db: psycopg.Connection[DictRow], db_settings: Settings
) -> None:
    # Statement 5 (docs/02, section 5) with two relays racing on two
    # connections, the pattern in test_fan_in_race.py. SKIP LOCKED is the
    # manual's own suggestion for many consumers of a queue-like table
    # (ADR-006), so neither relay waits on the other and no row goes twice.
    consultation_id = make_consultation(db, make_department(db), status="processing")
    owed = [make_outbox_row(db, consultation_id) for _ in range(OWED)]
    # Committed, so the relays' connections can see the rows.
    db.commit()

    # With a timeout, a thread that fails before the barrier breaks it for
    # the other and the test fails loudly instead of the pool joining for ever.
    barrier = threading.Barrier(RELAYS, timeout=30)

    def relay() -> int:
        with store.connect(db_settings) as conn:
            barrier.wait()
            return reconciler.relay(conn)

    with ThreadPoolExecutor(max_workers=RELAYS) as pool:
        futures = [pool.submit(relay) for _ in range(RELAYS)]
    # Every failure, not just the first future's, so the root cause shows
    # rather than the BrokenBarrierError the other raises after it.
    failures = [f.exception() for f in futures if f.exception() is not None]
    assert failures == []
    sent = [f.result() for f in futures]

    # Every row sent once: the two counts make thirty, and neither relay
    # took more than its twenty.
    assert sum(sent) == OWED
    assert max(sent) <= RELAY_LIMIT
    # Each row sent, with the fake Notify's reference and a time. The
    # reference is the outbox id, as ADR-006 has Notify's reference set.
    rows = db.execute(
        "SELECT id, status, notify_id, sent_at IS NOT NULL AS stamped FROM notification_outbox"
        " ORDER BY id"
    ).fetchall()
    assert rows == [
        {"id": row_id, "status": "sent", "notify_id": f"fake-notify-{row_id}", "stamped": True}
        for row_id in owed
    ]

    # Nothing is owed now, so a third relay sends nothing.
    assert reconciler.relay(db) == 0
