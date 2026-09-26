"""Fakes for the model boundary.

RecordingLLM is consult.fake_model.OfflineModel under the name the tests
have always used: it answers every prompt well and keeps each one, so a
test can assert what the model was shown. FakeLLM answers with what it was
told to: literal text, one of the ways a reply can be wrong
(THREAT_MODEL.md, row 3: ids missing, ids added, a label outside the enum,
malformed JSON, and prose where JSON was asked for), each built from the
good reply for the prompt it's answering so the fault is exact, for all
three shapes consult/replies.py validates, or a scripted llm.GatewayError,
raised rather than answered (docs/02, section 9).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from enum import StrEnum

from consult.fake_model import (
    OfflineModel,
    completion,
    good_assignments,
    good_condensation,
    good_themes,
)
from consult.llm import Completion, GatewayError, Prompt

RecordingLLM = OfflineModel


class Fault(StrEnum):
    NONE = "none"
    OUT_OF_ENUM_LABEL = "out_of_enum_label"
    DROPPED_ID = "dropped_id"
    EXTRA_ID = "extra_id"
    DUPLICATED_ID = "duplicated_id"
    PROSE = "prose"
    MALFORMED_JSON = "malformed_json"


class ScriptExhaustedError(Exception):
    """The fake was called more times than it was scripted for."""


def generation_reply_text(prompt: Prompt, fault: Fault) -> str:
    themes = good_themes(prompt)
    if fault is Fault.OUT_OF_ENUM_LABEL:
        themes[0]["key"] = "not a key"
    elif fault is Fault.DROPPED_ID:
        del themes[0]["label"]
    elif fault is Fault.EXTRA_ID:
        themes[0]["confidence"] = 0.9
    elif fault is Fault.DUPLICATED_ID:
        themes[1]["key"] = themes[0]["key"]
    elif fault is Fault.PROSE:
        return "Certainly! Here are the themes I found in the responses you sent."
    text = json.dumps({"themes": themes})
    if fault is Fault.MALFORMED_JSON:
        return text[:-2]
    return text


def condensation_reply_text(prompt: Prompt, fault: Fault) -> str:
    themes = good_condensation(prompt)
    if fault is Fault.OUT_OF_ENUM_LABEL:
        themes[0]["key"] = "not a key"
    elif fault is Fault.DROPPED_ID:
        del themes[0]["label"]
    elif fault is Fault.EXTRA_ID:
        themes[0]["confidence"] = 0.9
    elif fault is Fault.DUPLICATED_ID:
        themes.append(dict(themes[0]))
    elif fault is Fault.PROSE:
        return "Certainly! Here are the themes I found in the responses you sent."
    text = json.dumps({"themes": themes})
    if fault is Fault.MALFORMED_JSON:
        return text[:-2]
    return text


def reply_text(prompt: Prompt, fault: Fault) -> str:
    if not prompt.theme_keys and not prompt.answer_ids:
        return condensation_reply_text(prompt, fault)
    if not prompt.theme_keys:
        return generation_reply_text(prompt, fault)
    assignments = good_assignments(prompt)
    if fault is Fault.OUT_OF_ENUM_LABEL and assignments:
        assignments[0]["theme_keys"] = ["NOT_A_THEME_KEY"]
    elif fault is Fault.DROPPED_ID and assignments:
        assignments.pop()
    elif fault is Fault.EXTRA_ID:
        unsent = max(prompt.answer_ids, default=0) + 1
        assignments.append({"answer_id": unsent, "theme_keys": list(prompt.theme_keys[:1])})
    elif fault is Fault.DUPLICATED_ID and assignments:
        assignments.append(dict(assignments[0]))
    elif fault is Fault.PROSE:
        return "Certainly! Here are the themes I found in the responses you sent."
    text = json.dumps({"assignments": assignments})
    if fault is Fault.MALFORMED_JSON:
        return text[:-2]
    return text


class FakeLLM:
    """Replies in the order scripted; raises once the script runs out, so a
    test that calls the model more often than it meant to fails instead of
    passing on a default. A scripted GatewayError is raised rather than
    turned into a reply, so a test can drive worker.call_with_backoff
    exactly as a real gateway saying no would.

    `__init__` takes `script` as a `Sequence` and copies it into a `list`
    of its own, rather than being the plain dataclass-generated `__init__`
    a `list[str | Fault]` field would need: a list is invariant in its
    element type, so every test that already typed a script that way
    (test_mapping.py has two) would stop matching a field widened to
    include GatewayError. A Sequence is read-only and so covariant, and
    is never mutated here; only the copy is, with `pop(0)`.
    """

    def __init__(
        self,
        script: Sequence[str | Fault | GatewayError],
        prompts: list[Prompt] | None = None,
    ) -> None:
        self.script: list[str | Fault | GatewayError] = list(script)
        self.prompts: list[Prompt] = prompts if prompts is not None else []

    def complete(self, prompt: Prompt) -> Completion:
        self.prompts.append(prompt)
        if not self.script:
            raise ScriptExhaustedError(f"no reply scripted for call {len(self.prompts)}")
        step = self.script.pop(0)
        if isinstance(step, GatewayError):
            raise step
        text = reply_text(prompt, step) if isinstance(step, Fault) else step
        return completion(text, prompt, len(self.prompts))
