"""Log lines carry ids, counts, durations, states and codes. Never text.

THREAT_MODEL.md section 2 is the policy and docs/06 section 2.5 tags it a
MUST. This module is the control on the one path that's ours to control,
and it works by refusal: a field reaches a line only if its name says what
it is and its value has the shape of a token, the message has to be an
event name rather than a sentence, and an exception contributes its class
and never its message. So `log_event(log, "batch_rejected", answer=text)`
logs nothing rather than an answer, and a stray
`logger.error("failed: %s", body)` logs a marker and the logger's name,
which is enough to go and find the line.

Named `logs`, not `logging`, so it can't shadow the standard library.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

# What a field's name has to say for the field to be rendered.
_ALLOWED_NAME = re.compile(
    r"^(?:"
    r"[a-z0-9_]*_id|[a-z0-9_]*_ids"
    r"|[a-z0-9_]*_count|count|[a-z0-9_]*_no|attempts|version|ordinal"
    r"|[a-z0-9_]*_ms|[a-z0-9_]*_seconds"
    r"|status|from_status|to_status|kind|stage"
    r"|[a-z0-9_]*_code|exception"
    r"|tokens_in|tokens_out|tokens_cached|cost_pence"
    r")$"
)
# What a value has to look like: a token, never a sentence. Sixty-four
# characters holds a uuid, a request id and a worker name with room over.
_ALLOWED_VALUE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,64}$")
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
MESSAGE_DROPPED = "message_dropped"


def _render(value: object) -> str | None:
    if isinstance(value, Enum):
        value = value.value
    if value is None or isinstance(value, bool | int | float | UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="milliseconds")
    if isinstance(value, str) and _ALLOWED_VALUE.match(value):
        return value
    return None


class Formatter(logging.Formatter):
    """One line: a timestamp, the level, the logger, the event, then the
    fields that pass, sorted by name so a line reads the same every time."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds")
        message = record.getMessage()
        event = message if _EVENT_NAME.match(message) else MESSAGE_DROPPED
        fields: dict[str, object] = {}
        given = getattr(record, "fields", None)
        if isinstance(given, Mapping):
            fields.update(given)
        if record.exc_info and record.exc_info[0] is not None:
            fields["exception"] = record.exc_info[0].__name__
        parts = [stamp.replace("+00:00", "Z"), record.levelname, record.name, event]
        for name, value in sorted(fields.items()):
            rendered = _render(value) if _ALLOWED_NAME.match(name) else None
            if rendered is not None:
                parts.append(f"{name}={rendered}")
        return " ".join(parts)


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    exc_info: BaseException | bool | None = None,
    **fields: object,
) -> None:
    """The one way to log: an event name and named fields, never a sentence."""
    logger.log(level, event, exc_info=exc_info, extra={"fields": fields})


def configure(level: int = logging.INFO) -> None:
    """One handler on the root logger with this formatter, replacing any
    earlier one, so calling it twice leaves one."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler.formatter, Formatter):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(Formatter())
    root.addHandler(handler)
    root.setLevel(level)
