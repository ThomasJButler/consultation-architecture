"""Model output, held to the schema, the enum and the ids before anything
reads it (CLAUDE.md, rule 9; THREAT_MODEL.md, row 3).

The gateway asks for structured output; this module doesn't trust that it
got it. A mapping reply has to be an object with an `assignments` list
whose items carry exactly an integer `answer_id` and a list of string
`theme_keys`, every key from the enum the prompt carried, and every answer
id the prompt sent has to come back exactly once with none added (docs/02,
step 9's two-way check). A generation reply has to be an object with a
`themes` list of key, label and description, keys well-formed and unique.
A reply that fails is a ReplyError carrying job.error's code and a reason
from a fixed list, never a word of the reply, because there is no column
for one (docs/02, section 3.4). The checks here mirror the schemas in
consult/prompts.py by hand rather than through a validator library, so
the rules the model is told are the rules the code enforces.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from consult.errors import ErrorCode
from consult.llm import Completion, Prompt
from consult.prompts import KEY_PATTERN

_KEY = re.compile(KEY_PATTERN)


class Reason(StrEnum):
    NOT_JSON = "not_json"
    NOT_AN_OBJECT = "not_an_object"
    WRONG_SHAPE = "wrong_shape"
    IDS_MISSING = "ids_missing"
    IDS_UNSENT = "ids_unsent"
    IDS_REPEATED = "ids_repeated"
    LABEL_OUTSIDE_ENUM = "label_outside_enum"
    KEY_MALFORMED = "key_malformed"
    KEY_REPEATED = "key_repeated"
    NO_THEMES = "no_themes"


class ReplyError(Exception):
    """A reply that failed a check. `code` is what job.error takes; `reason`
    and `count` are what a log line may carry. The reply's text is not here."""

    code = ErrorCode.MODEL_OUTPUT_INVALID

    def __init__(self, reason: Reason, count: int = 0) -> None:
        self.reason = reason
        self.count = count
        super().__init__(f"{self.code.value}: {reason.value} ({count})")


@dataclass(frozen=True)
class Assignment:
    answer_id: int
    theme_keys: tuple[str, ...]


@dataclass(frozen=True)
class ProposedTheme:
    key: str
    label: str
    description: str


def _object(text: str) -> dict[str, object]:
    try:
        reply = json.loads(text)
    except ValueError as exc:
        raise ReplyError(Reason.NOT_JSON) from exc
    if not isinstance(reply, dict):
        raise ReplyError(Reason.NOT_AN_OBJECT)
    return reply


def _items(reply: dict[str, object], name: str, fields: tuple[str, ...]) -> list[dict[str, object]]:
    """The named list of objects, each with exactly the fields given."""
    if set(reply) != {name}:
        raise ReplyError(Reason.WRONG_SHAPE)
    items = reply[name]
    if not isinstance(items, list):
        raise ReplyError(Reason.WRONG_SHAPE)
    checked: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict) or set(item) != set(fields):
            raise ReplyError(Reason.WRONG_SHAPE, len(items))
        checked.append(item)
    return checked


def parse_assignments(completion: Completion, prompt: Prompt) -> tuple[Assignment, ...]:
    """The mapping reply as typed assignments, or a ReplyError."""
    items = _items(_object(completion.text), "assignments", ("answer_id", "theme_keys"))
    assignments: list[Assignment] = []
    for item in items:
        answer_id, theme_keys = item["answer_id"], item["theme_keys"]
        # bool is a subclass of int in Python and not an integer to the schema.
        if isinstance(answer_id, bool) or not isinstance(answer_id, int):
            raise ReplyError(Reason.WRONG_SHAPE, len(items))
        if not isinstance(theme_keys, list) or not all(isinstance(k, str) for k in theme_keys):
            raise ReplyError(Reason.WRONG_SHAPE, len(items))
        assignments.append(Assignment(answer_id, tuple(dict.fromkeys(theme_keys))))
    # The two-way check: every id sent comes back exactly once, and nothing
    # else does. Which way it failed is the reason.
    sent = Counter(prompt.answer_ids)
    returned = Counter(assignment.answer_id for assignment in assignments)
    repeated = sum(count - 1 for count in returned.values() if count > 1)
    if repeated:
        raise ReplyError(Reason.IDS_REPEATED, repeated)
    unsent = returned.keys() - sent.keys()
    if unsent:
        raise ReplyError(Reason.IDS_UNSENT, len(unsent))
    missing = sent.keys() - returned.keys()
    if missing:
        raise ReplyError(Reason.IDS_MISSING, len(missing))
    # Labels are an enum of theme keys and nothing outside it is a label.
    enum = set(prompt.theme_keys)
    outside = sum(1 for a in assignments for key in a.theme_keys if key not in enum)
    if outside:
        raise ReplyError(Reason.LABEL_OUTSIDE_ENUM, outside)
    return tuple(assignments)


def parse_themes(completion: Completion) -> tuple[ProposedTheme, ...]:
    """The generation reply as typed themes, or a ReplyError."""
    items = _items(_object(completion.text), "themes", ("key", "label", "description"))
    if not items:
        raise ReplyError(Reason.NO_THEMES)
    themes: list[ProposedTheme] = []
    for item in items:
        key, label, description = item["key"], item["label"], item["description"]
        if not (isinstance(key, str) and isinstance(label, str) and isinstance(description, str)):
            raise ReplyError(Reason.WRONG_SHAPE, len(items))
        if not label:
            raise ReplyError(Reason.WRONG_SHAPE, len(items))
        if not _KEY.fullmatch(key):
            raise ReplyError(Reason.KEY_MALFORMED, len(items))
        themes.append(ProposedTheme(key, label, description))
    repeated = sum(count - 1 for count in Counter(t.key for t in themes).values() if count > 1)
    if repeated:
        raise ReplyError(Reason.KEY_REPEATED, repeated)
    return tuple(themes)
