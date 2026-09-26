"""An offline stand-in for the model, behind the `LLM` protocol (ADR-005).

The proof-of-concept runs with no model (poc/README.md): this one answers
every prompt well and keeps what it was shown, so the mechanics around
the call can be run and tested without a gateway. It reads the prompt to
pick a shape: theme keys mean a mapping (or preview) call, answer ids
with no keys a generation call, neither a condensation call. Themes are
named from the batch so a fake condensation has something to fold, and
every answer is labelled with the first key of the enum. The faults a
reply can carry live in tests/fakes.py, built on the good replies here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from consult.llm import Completion, Prompt

STEMS = (("SAFETY", "Safety"), ("PARKING", "Parking"), ("ACCESS", "Access"))


def good_assignments(prompt: Prompt) -> list[dict[str, object]]:
    """Every id once, each labelled with the first key of the enum."""
    key = prompt.theme_keys[0] if prompt.theme_keys else "OTHER"
    return [{"answer_id": answer_id, "theme_keys": [key]} for answer_id in prompt.answer_ids]


def good_themes(prompt: Prompt) -> list[dict[str, object]]:
    """Three themes per batch, suffixed with the batch's first answer id."""
    base = min(prompt.answer_ids, default=0)
    return [
        {"key": f"{stem}_{base}", "label": f"{label} ({base})", "description": f"{label} raised"}
        for stem, label in STEMS
    ]


def good_condensation(prompt: Prompt) -> list[dict[str, object]]:
    """The candidates in the prompt's data folded by key stem: SAFETY_1 and
    SAFETY_51 become SAFETY. Grouping is the mechanic under proof, not the
    judgement, so a stem is enough."""
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


def good_reply_text(prompt: Prompt) -> str:
    if prompt.theme_keys:
        return json.dumps({"assignments": good_assignments(prompt)})
    if prompt.answer_ids:
        return json.dumps({"themes": good_themes(prompt)})
    return json.dumps({"themes": good_condensation(prompt)})


def completion(text: str, prompt: Prompt, call_no: int) -> Completion:
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
class OfflineModel:
    """Answers well and remembers every prompt, so a test can assert what
    the model was shown and, more to the point, what it wasn't."""

    prompts: list[Prompt] = field(default_factory=list)

    def complete(self, prompt: Prompt) -> Completion:
        self.prompts.append(prompt)
        return completion(good_reply_text(prompt), prompt, len(self.prompts))
