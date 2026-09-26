"""Fakes for the model boundary.

RecordingLLM answers every prompt well and keeps each one, so a test can
assert what the model was shown. FakeLLM answers with what it was told to:
literal text, or one of the ways a reply can be wrong (THREAT_MODEL.md,
row 3: ids missing, ids added, a label outside the enum, malformed JSON,
and prose where JSON was asked for), each built from the prompt it's
answering so the fault is exact.

The fakes speak both shapes consult/replies.py validates, and read the
prompt to pick one. A prompt with theme keys is a mapping (or preview)
call and gets `{"assignments": [{"answer_id": <int>, "theme_keys": [<key>,
...]}, ...]}`, every id sent back once, every key from the enum. A prompt
with answer ids and no theme keys is a generation call and gets `{"themes":
[{"key", "label", "description"}, ...]}`, three themes named from the
batch's first id so condensation has something to merge. A prompt with
neither is a condensation call and gets the candidates in its data folded
by key stem, each with the keys it merges. Each fault is built from the
prompt it's answering so the fault is exact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum

from consult.llm import Completion, Prompt


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


def good_assignments(prompt: Prompt) -> list[dict[str, object]]:
    """Every id once, each labelled with the first key of the enum."""
    key = prompt.theme_keys[0] if prompt.theme_keys else "OTHER"
    return [{"answer_id": answer_id, "theme_keys": [key]} for answer_id in prompt.answer_ids]


def good_themes(prompt: Prompt) -> list[dict[str, object]]:
    """Three themes per batch, suffixed with the batch's first answer id, so
    a fake condensation can fold SAFETY_1 and SAFETY_51 into SAFETY."""
    base = min(prompt.answer_ids, default=0)
    return [
        {"key": f"{stem}_{base}", "label": f"{label} ({base})", "description": f"{label} raised"}
        for stem, label in (("SAFETY", "Safety"), ("PARKING", "Parking"), ("ACCESS", "Access"))
    ]


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


def good_condensation(prompt: Prompt) -> list[dict[str, object]]:
    """The candidates in the prompt's data folded by key stem: SAFETY_1 and
    SAFETY_51 become SAFETY. Grouping is the mechanic under proof in the
    condensation tests, not the judgement, so a stem is enough."""
    groups: dict[str, list[str]] = {}
    descriptions: dict[str, str] = {}
    for candidate in json.loads(prompt.user):
        stem = str(candidate["key"]).rsplit("_", 1)[0]
        groups.setdefault(stem, []).append(str(candidate["key"]))
        descriptions.setdefault(stem, str(candidate["description"]))
    return [
        {"key": stem, "label": stem.capitalize(), "description": descriptions[stem], "merges": keys}
        for stem, keys in groups.items()
    ]


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


def _completion(text: str, prompt: Prompt, call_no: int) -> Completion:
    # Four characters a token is the usual rough figure and nothing here
    # depends on it being right; the ledger columns just need a number.
    return Completion(
        text=text,
        tokens_in=max(1, (len(prompt.system) + len(prompt.user)) // 4),
        tokens_out=max(1, len(text) // 4),
        provider_request_id=f"fake-request-{call_no}",
        trace_id=f"fake-trace-{call_no}",
    )


@dataclass
class RecordingLLM:
    prompts: list[Prompt] = field(default_factory=list)

    def complete(self, prompt: Prompt) -> Completion:
        self.prompts.append(prompt)
        return _completion(reply_text(prompt, Fault.NONE), prompt, len(self.prompts))


@dataclass
class FakeLLM:
    """Replies in the order scripted; raises once the script runs out, so a
    test that calls the model more often than it meant to fails instead of
    passing on a default."""

    script: list[str | Fault]
    prompts: list[Prompt] = field(default_factory=list)

    def complete(self, prompt: Prompt) -> Completion:
        self.prompts.append(prompt)
        if not self.script:
            raise ScriptExhaustedError(f"no reply scripted for call {len(self.prompts)}")
        step = self.script.pop(0)
        text = reply_text(prompt, step) if isinstance(step, Fault) else step
        return _completion(text, prompt, len(self.prompts))
