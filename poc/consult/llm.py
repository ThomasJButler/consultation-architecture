"""The model boundary. Everything that talks to a model goes through `LLM`.

In production the gateway sits behind this protocol (ADR-005); in the
tests a fake from tests/fakes.py does. What crosses it is a Prompt whose
data is JSON-encoded after a preamble saying instructions inside it are
data (docs/02, step 9), and a Completion whose text is untrusted until the
caller has parsed and validated it in code (CLAUDE.md, rule 9). Nothing
here validates anything: that's the caller's job, and PR-07 pins it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from consult.errors import ErrorCode


@dataclass(frozen=True)
class Prompt:
    """One call's input.

    `system` and `user` are what the model reads. `answer_ids` and
    `theme_keys` are the same facts in structured form: the ids the batch
    carries, so the two-way check can hold the reply to them, and the enum
    the reply must label with. A fake reads them to build a reply; the
    worker reads them to validate one.
    """

    model_alias: str
    system: str
    user: str
    answer_ids: tuple[int, ...] = ()
    theme_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class Completion:
    """One call's output: the text, untrusted, and the ledger fields.

    Text and nothing more, because the caller parses and validates it. The
    token counts and the provider's request id are what `job` and
    `job_batch` record (docs/04); the request id is also all `job.error`
    ever holds of a failure, never a message body (THREAT_MODEL.md,
    section 2).
    """

    text: str
    tokens_in: int = 0
    tokens_cached: int = 0
    tokens_out: int = 0
    provider_request_id: str | None = None
    trace_id: str | None = None


class LLM(Protocol):
    def complete(self, prompt: Prompt) -> Completion: ...


class GatewayError(Exception):
    """A 429 or a 5xx from the gateway (ADR-005; docs/02, section 9).

    `code` and `request_id` are what `job.error` and a log line take.
    `message` is the provider's text, kept off `args` and off `str(exc)`
    so nothing but the code and the request id can ride an exception
    chain, a traceback or a log line (CLAUDE.md, rule 8: `job.error`
    stores a code and a request id, never a message body).
    """

    def __init__(self, code: ErrorCode, request_id: str | None = None, message: str = "") -> None:
        self.code = code
        self.request_id = request_id
        self._message = message
        super().__init__(f"{code.value} request_id={request_id}")
