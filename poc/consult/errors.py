"""The fixed vocabulary `job.error_code` takes.

THREAT_MODEL.md section 2, line 3: a failed job stores a code from a fixed
vocabulary and the provider's request id, and there is no column for a
message body. A CHECK in schema.sql holds the column to this same list, so
a message body in the code column is refused by the database and not just
by convention; a test holds the two lists equal in both directions.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    # The gateway said no. docs/02 section 9 handles a 429 or a 5xx with
    # backoff, and a spend-cap 429 pages a person. A timeout is treated
    # like a 5xx and any other 4xx as the request's fault: that reading is
    # this module's, not the design's.
    GATEWAY_TIMEOUT = "gateway_timeout"
    GATEWAY_RATE_LIMITED = "gateway_rate_limited"
    GATEWAY_UNAVAILABLE = "gateway_unavailable"
    GATEWAY_REJECTED = "gateway_rejected"
    # The reply failed the schema, the enum or the two-way check at batch
    # size one, so the answer is in the unprocessable bucket (docs/02, step 9).
    MODEL_OUTPUT_INVALID = "model_output_invalid"
    # The fence was stale at a write: another worker holds the lease (ADR-002).
    LEASE_LOST = "lease_lost"
    # The stage job's validator found an error, not a warning (docs/02, 3.2).
    INPUT_INVALID = "input_invalid"
    # Anything else. The exception's class goes to the log; its message goes nowhere.
    WORKER_ERROR = "worker_error"
