"""What `dispatch.select` promises (docs/02, step 4).

Pure: no psycopg import, no db fixture, so this module runs under
`pytest -m 'not db'` (test_repo_rules.py bans `pytestmark = pytest.mark.db`
on a module that doesn't need one). The database half of dispatch, the
locked `UPDATE` that acts on what `select` picks, is
`tests/test_dispatch_db.py`.

Every expected list below is worked out by hand from docs/02 step 4's
caps (six a department, four a consultation, twenty service-wide,
round-robin across departments when contended), never read off the code
under test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from consult.dispatch import JobCaps, LiveCounts, Pending, select

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _job(n: int, department_id: UUID, consultation_id: UUID, created_at: datetime) -> Pending:
    return Pending(
        job_id=UUID(int=n),
        department_id=department_id,
        consultation_id=consultation_id,
        created_at=created_at,
    )


def test_select_round_robins_under_the_three_caps() -> None:
    # One department, two consultations, one job already live in the
    # first: the department cap (6) counts that live job, so only five
    # more fit, and the consultation cap (4) stops the first consultation
    # after three more (it already holds one). A job blocked only by its
    # consultation's cap is skipped for the next job of the same
    # department (docs/02 step 4), so 1004 and 1005 are passed over for
    # 2001 once C1 is full.
    dept_d = UUID(int=1)
    cons_c1 = UUID(int=11)
    cons_c2 = UUID(int=12)
    pending_d = [
        _job(1001, dept_d, cons_c1, T0),
        _job(1002, dept_d, cons_c1, T0),
        _job(1003, dept_d, cons_c1, T0),
        _job(1004, dept_d, cons_c1, T0),
        _job(1005, dept_d, cons_c1, T0),
        _job(2001, dept_d, cons_c2, T0),
        _job(2002, dept_d, cons_c2, T0),
        _job(2003, dept_d, cons_c2, T0),
    ]
    live_d = LiveCounts(per_department={dept_d: 1}, per_consultation={cons_c1: 1}, in_all=1)
    caps_d = JobCaps(per_department={dept_d: 6}, per_consultation=4, in_all=20)
    assert select(pending_d, live_d, caps_d) == [
        UUID(int=n) for n in (1001, 1002, 1003, 2001, 2002)
    ]

    # One department, one consultation, no live jobs: the consultation cap
    # (4) binds before the department cap (6) ever could, and once it's
    # hit there's no second consultation in the department to draw from,
    # so the remaining three pending jobs go unpicked.
    dept_e = UUID(int=2)
    cons_c3 = UUID(int=21)
    pending_e = [_job(3000 + i, dept_e, cons_c3, T0) for i in range(7)]
    live_e = LiveCounts(per_department={}, per_consultation={}, in_all=0)
    caps_e = JobCaps(per_department={dept_e: 6}, per_consultation=4, in_all=20)
    assert select(pending_e, live_e, caps_e) == [UUID(int=3000 + i) for i in range(4)]

    # Oldest first, timestamp before id: j_b was created before j_d and
    # j_a despite carrying a far larger id, so it's picked first among
    # them. j_c and j_b share a timestamp, so the id breaks the tie.
    dept_f = UUID(int=3)
    cons_c4 = UUID(int=31)
    j_a = _job(9001, dept_f, cons_c4, T0 + timedelta(seconds=5))
    j_b = _job(9099, dept_f, cons_c4, T0 + timedelta(seconds=1))
    j_c = _job(9005, dept_f, cons_c4, T0 + timedelta(seconds=1))
    j_d = _job(9002, dept_f, cons_c4, T0 + timedelta(seconds=3))
    caps_f = JobCaps(per_department={dept_f: 10}, per_consultation=10, in_all=10)
    live_f = LiveCounts(per_department={}, per_consultation={}, in_all=0)
    assert select([j_a, j_b, j_c, j_d], live_f, caps_f) == [
        j_c.job_id,
        j_b.job_id,
        j_d.job_id,
        j_a.job_id,
    ]

    # One ingest gives every job the same `created_at` (docs/02 step 3a:
    # ingest runs as one transaction), so with department and consultation
    # caps wide open, only the service-wide cap (20) and the id tiebreak
    # decide the pick: the twenty lowest ids, in order, and no more.
    dept_g = UUID(int=4)
    cons_c5 = UUID(int=41)
    pending_g = [_job(5000 + i, dept_g, cons_c5, T0) for i in range(25)]
    caps_g = JobCaps(per_department={dept_g: 25}, per_consultation=25, in_all=20)
    live_g = LiveCounts(per_department={}, per_consultation={}, in_all=0)
    assert select(pending_g, live_g, caps_g) == [UUID(int=5000 + i) for i in range(20)]

    # Two departments, each with more pending jobs than the three service
    # -wide slots leave room for: the pick alternates between them,
    # starting with the one whose oldest pending job is oldest (dept_e1's
    # first job predates dept_e2's), and stops mid-round when the
    # service-wide cap is reached.
    dept_e1 = UUID(int=21)
    dept_e2 = UUID(int=22)
    cons_e1 = UUID(int=211)
    cons_e2 = UUID(int=221)
    jobs_e1 = [_job(6001 + i, dept_e1, cons_e1, T0 + timedelta(seconds=10 * i)) for i in range(4)]
    jobs_e2 = [
        _job(6101 + i, dept_e2, cons_e2, T0 + timedelta(seconds=5 + 10 * i)) for i in range(4)
    ]
    caps_rr = JobCaps(per_department={dept_e1: 10, dept_e2: 10}, per_consultation=10, in_all=3)
    live_rr = LiveCounts(per_department={}, per_consultation={}, in_all=0)
    assert select(jobs_e1 + jobs_e2, live_rr, caps_rr) == [
        UUID(int=6001),
        UUID(int=6101),
        UUID(int=6002),
    ]

    # Every slot the one contending department has is already live: the
    # pick is empty even though jobs are pending.
    dept_h = UUID(int=5)
    cons_h = UUID(int=51)
    pending_h = [_job(7001 + i, dept_h, cons_h, T0) for i in range(3)]
    caps_h = JobCaps(per_department={dept_h: 6}, per_consultation=4, in_all=20)
    live_h = LiveCounts(per_department={dept_h: 6}, per_consultation={}, in_all=6)
    assert select(pending_h, live_h, caps_h) == []
