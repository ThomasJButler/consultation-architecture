"""What the log formatter promises: ids, counts, durations, states and codes
reach a line, and nothing else does, whatever a caller passes.

THREAT_MODEL.md section 2 is the policy. The control is deny by default: a
field is rendered only if its name says what it is, its value has the
shape of a token, the message is an event name rather than a sentence,
and an exception contributes its class and never its message.
"""

from __future__ import annotations

import io
import logging
import sys
import time
from uuid import UUID

import pytest

from consult.logs import Formatter, configure, log_event

JOB_ID = UUID("0b6a5f3c-2d1e-4f7a-9c8b-1a2b3c4d5e6f")


def render(
    formatter: Formatter,
    event: str,
    /,
    *,
    level: int = logging.INFO,
    exc_info: tuple[type[BaseException], BaseException, None] | None = None,
    **fields: object,
) -> str:
    record = logging.makeLogRecord(
        {
            "name": "consult.worker",
            "levelno": level,
            "levelname": logging.getLevelName(level),
            "msg": event,
            "created": time.time(),
            "exc_info": exc_info,
            "fields": fields,
        }
    )
    return formatter.format(record)


def test_the_formatter_keeps_ids_counts_durations_states_and_codes() -> None:
    line = render(
        Formatter(),
        "question_finished",
        job_id=JOB_ID,
        batch_no=7,
        attempts=2,
        answer_count=50,
        duration_ms=812.4,
        from_status="finding_themes",
        to_status="themes_ready",
        kind="find_themes",
        error_code=None,
        tokens_in=12000,
        cost_pence=3,
    )
    assert line.endswith(
        "INFO consult.worker question_finished answer_count=50 attempts=2 batch_no=7"
        " cost_pence=3 duration_ms=812.4 error_code=None from_status=finding_themes"
        f" job_id={JOB_ID} kind=find_themes to_status=themes_ready tokens_in=12000"
    )


def test_the_formatter_drops_fields_named_as_text() -> None:
    line = render(
        Formatter(),
        "batch_rejected",
        job_id=JOB_ID,
        answer="short",
        prompt="short",
        completion="short",
        label="short",
        message="short",
        value_text="short",
        question_text="short",
        email="a@example.org",
        filename="responses.csv",
        error="short",
        text="short",
    )
    assert line.endswith(f"batch_rejected job_id={JOB_ID}")
    assert "short" not in line


def test_the_formatter_drops_a_value_shaped_like_prose_whatever_its_name() -> None:
    line = render(
        Formatter(),
        "job_failed",
        error_code="502 Bad Gateway while sending the prompt",
        status="failed_retryable",
        worker_id="worker-1.ip-10-0-0-7",
        answer_ids=[1, 2, 3],
        # An identifier column in an upload is an email address often enough
        # (docs/00) that an id-named field can carry one; the shape rule, not
        # the name rule, is what has to drop it.
        external_id="jane.doe@example.org",
    )
    assert line.endswith("job_failed status=failed_retryable worker_id=worker-1.ip-10-0-0-7")
    assert "@" not in line


def test_the_formatter_keeps_to_one_line_whatever_arrives() -> None:
    # `$` in a Python regex also matches before a trailing newline, so a
    # value read from a header or a file with its newline still on would
    # split the entry in two unless the match is a full match.
    line = render(
        Formatter(),
        "job_failed\n",
        job_id=JOB_ID,
        provider_request_id="req_01J8ZW4\n",
        attempts=1,
    )
    assert "\n" not in line
    assert line.endswith(f"message_dropped attempts=1 job_id={JOB_ID}")


def test_a_mismatched_call_logs_a_marker_and_prints_nothing_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # logging.Handler.emit catches an exception from format() and, with the
    # stdlib default raiseExceptions, prints the message and its arguments
    # to stderr. That path would carry an answer past the formatter.
    stream = io.StringIO()
    logger = logging.getLogger("consult.test_logs.mismatch")
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(Formatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.error("failed %s %s", "The towpath is underwater, said respondent042@example.org")
    finally:
        logger.removeHandler(handler)
    assert capsys.readouterr().err == ""
    assert stream.getvalue().rstrip().endswith("ERROR consult.test_logs.mismatch message_dropped")
    assert "towpath" not in stream.getvalue()


def test_the_formatter_names_an_exception_class_and_never_its_message() -> None:
    try:
        raise ValueError("the towpath is underwater every winter")
    except ValueError as exc:
        exc_info = (type(exc), exc, None)
    line = render(Formatter(), "job_failed", job_id=JOB_ID, exc_info=exc_info)
    assert line.endswith(f"job_failed exception=ValueError job_id={JOB_ID}")
    assert "towpath" not in line
    assert "\n" not in line


def test_the_formatter_replaces_a_sentence_with_a_marker() -> None:
    line = render(Formatter(), "Failed to tag: the towpath is underwater", job_id=JOB_ID)
    assert line.endswith(f"message_dropped job_id={JOB_ID}")
    assert "towpath" not in line


def test_log_event_puts_the_fields_on_the_record() -> None:
    stream = io.StringIO()
    logger = logging.getLogger("consult.test_logs")
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(Formatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        log_event(logger, "job_claimed", job_id=JOB_ID, attempts=2, answer="dropped")
    finally:
        logger.removeHandler(handler)
    assert (
        stream.getvalue()
        .rstrip()
        .endswith(f"INFO consult.test_logs job_claimed attempts=2 job_id={JOB_ID}")
    )


def test_configure_installs_one_handler_with_the_formatter() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        configure(logging.WARNING)
        configure(logging.WARNING)
        ours = [h for h in root.handlers if isinstance(h.formatter, Formatter)]
        assert len(ours) == 1
        assert isinstance(ours[0], logging.StreamHandler)
        assert ours[0].stream is sys.stderr
        assert root.level == logging.WARNING
    finally:
        root.handlers[:] = before
