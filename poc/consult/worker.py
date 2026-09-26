"""The model boundary the worker calls through, and what it does once a
gateway failure has been given every chance.

`consult.llm.GatewayError` is a 429 or a 5xx from the gateway (ADR-005).
`BackingOff` wraps an `LLM` so `mapping.assign` and `themes.generate` need
no change (plan section 2, "consult/worker.py"): it retries a
`GatewayError` up to `BACKOFF_ATTEMPTS` times with full-jitter backoff
(docs/02, section 9; ADR-005's "one to sixty seconds, six attempts"),
running its `before_call` immediately ahead of every attempt so no
transaction is open while the gateway is slow or the backoff sleeps.
`now()` is transaction start (PostgreSQL 17 manual, 9.9.5; PR-05's
security review, docs/07 row 05), so a heartbeat stamped after a sleep
inside an open transaction is already stale by the sleep's length, and a
transaction held open across a call keeps the job row locked for the
call's duration, which blocks the reconciler's `fail_job`.

`record_gateway_failure` is what a caller runs once the retries are
spent: the code and the request id go on the job row under the fence,
never the provider's message (CLAUDE.md, rule 8), and the log line that
reports it carries the same two fields and nothing else
(THREAT_MODEL.md, section 2).
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

import psycopg
from psycopg.rows import DictRow

from consult import jobs, logs
from consult.jobs import Lease, LeaseLostError
from consult.llm import LLM, Completion, GatewayError, Prompt

logger = logging.getLogger(__name__)

# docs/02, section 9: "Full-jitter backoff, 1 to 60 s, six attempts."
BACKOFF_BASE_SECONDS = 1
BACKOFF_CAP_SECONDS = 60
BACKOFF_ATTEMPTS = 6


def backoff_seconds(attempt: int, rng: random.Random) -> float:
    """AWS's full jitter for the n-th retry: a draw from
    `[0, min(cap, base * 2**attempt)]` ("Exponential Backoff And Jitter",
    the formula docs/02 section 9 names as "full-jitter"), floored at one
    second, which is the design's own floor and not the formula's."""
    upper = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2**attempt)
    return max(1.0, rng.uniform(0, upper))


def call_with_backoff(
    llm: LLM,
    prompt: Prompt,
    *,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> Completion:
    """One call, retried on a `GatewayError` up to `BACKOFF_ATTEMPTS`
    times with `backoff_seconds` between attempts and no sleep after the
    last. The final attempt is unguarded: its error, if any, is left to
    propagate on its own, so nothing here re-raises it onto a chain that
    could carry its message."""
    if rng is None:
        # Spread across retries, not secrecy (themes.py's shuffle reads the
        # same way; docs/02, step 6).
        rng = random.Random()  # noqa: S311  # nosec B311
    for attempt in range(BACKOFF_ATTEMPTS - 1):
        try:
            return llm.complete(prompt)
        except GatewayError:
            sleep(backoff_seconds(attempt, rng))
    return llm.complete(prompt)


@dataclass
class _BeforeEachAttempt:
    """Runs `before_call` immediately ahead of one `complete`, so
    `call_with_backoff`'s loop gets it ahead of every attempt it makes and
    not just the first."""

    llm: LLM
    before_call: Callable[[], None] | None

    def complete(self, prompt: Prompt) -> Completion:
        if self.before_call is not None:
            self.before_call()
        return self.llm.complete(prompt)


@dataclass
class BackingOff:
    """The model boundary a worker hands to `mapping.assign` and
    `themes.generate`: it satisfies `LLM`, so neither stage changes, and
    it backs off on a `GatewayError` outside whatever transaction the
    caller had open. `before_call` is where the worker passes
    `conn.commit`, which is why no call and no sleep ever finds one
    (module docstring)."""

    llm: LLM
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random | None = None
    before_call: Callable[[], None] | None = None

    def complete(self, prompt: Prompt) -> Completion:
        wrapped = _BeforeEachAttempt(self.llm, self.before_call)
        return call_with_backoff(wrapped, prompt, sleep=self.sleep, rng=self.rng)


def record_gateway_failure(
    conn: psycopg.Connection[DictRow], lease: Lease, exc: GatewayError
) -> None:
    """What a caller runs once `call_with_backoff` gives up.

    Nothing needs discarding: the writes still pending at this point are
    a fenced heartbeat or a previous batch's rows, both of which stand on
    their own (the decision in plans/PR-08-poc-mapping-worker.md section
    2), so the rollback is for symmetry with the reply-error path, not
    because anything here is half-written. Then the code and the request
    id go on the job row under the fence, a commit, and a log line that
    carries the same two fields and never the provider's text.

    A stale fence surfaces as `LeaseLostError`, left to propagate with its
    own code: `cli._run_job`'s three patterns handle a find_themes job's
    failure the same way.
    """
    conn.rollback()
    try:
        jobs.record_failure(conn, lease, exc.code, provider_request_id=exc.request_id)
    except LeaseLostError:
        conn.rollback()
        raise
    conn.commit()
    logs.log_event(
        logger,
        "job_failed",
        job_id=lease.job_id,
        attempts=lease.fence,
        error_code=exc.code,
        provider_request_id=exc.request_id,
    )
