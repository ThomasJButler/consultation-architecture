"""What the reply validator promises (CLAUDE.md, rule 9; THREAT_MODEL.md,
row 3; docs/02, step 9).

Model output is untrusted until code has parsed it: the JSON has to be the
shape the schema asked for, every label has to be a key from the enum the
prompt carried, and every answer id the prompt sent has to come back
exactly once with nothing extra. A reply that fails is a code and a
reason from a fixed list, never the reply's text, because `job.error`
holds a code and a request id and nothing else.
"""

from __future__ import annotations

import json

import pytest

from consult.errors import ErrorCode
from consult.llm import Completion, Prompt
from consult.replies import (
    Assignment,
    ProposedTheme,
    Reason,
    ReplyError,
    parse_assignments,
    parse_themes,
)
from tests.fakes import FakeLLM, Fault, RecordingLLM

MAPPING = Prompt(
    model_alias="fake-model",
    system="s",
    user="[]",
    answer_ids=(1, 2, 3),
    theme_keys=("PARKING", "SAFETY", "OTHER"),
)
GENERATION = Prompt(model_alias="fake-model", system="s", user="[]", answer_ids=(1, 2, 3))
FAULTS = [fault for fault in Fault if fault is not Fault.NONE]


def refused_with(text: str, prompt: Prompt = MAPPING) -> Reason:
    with pytest.raises(ReplyError) as refused:
        parse_assignments(Completion(text=text), prompt)
    assert refused.value.code is ErrorCode.MODEL_OUTPUT_INVALID
    return refused.value.reason


def test_a_mapping_reply_is_validated_in_code() -> None:
    good = RecordingLLM().complete(MAPPING)
    assert parse_assignments(good, MAPPING) == (
        Assignment(1, ("PARKING",)),
        Assignment(2, ("PARKING",)),
        Assignment(3, ("PARKING",)),
    )

    # Every fault the fake can produce is refused with the code job.error
    # takes, and the message carries a reason and a count, never the reply.
    reasons: dict[Fault, Reason] = {}
    for fault in FAULTS:
        completion = FakeLLM([fault]).complete(MAPPING)
        with pytest.raises(ReplyError) as refused:
            parse_assignments(completion, MAPPING)
        assert refused.value.code is ErrorCode.MODEL_OUTPUT_INVALID
        assert "NOT_A_THEME_KEY" not in str(refused.value)
        assert "Certainly" not in str(refused.value)
        reasons[fault] = refused.value.reason
    assert reasons == {
        Fault.OUT_OF_ENUM_LABEL: Reason.LABEL_OUTSIDE_ENUM,
        Fault.DROPPED_ID: Reason.IDS_MISSING,
        Fault.EXTRA_ID: Reason.IDS_UNSENT,
        Fault.DUPLICATED_ID: Reason.IDS_REPEATED,
        Fault.PROSE: Reason.NOT_JSON,
        Fault.MALFORMED_JSON: Reason.NOT_JSON,
    }

    # The shape checks the schema promises, hand-built: the wrong top-level
    # type, a missing or extra property, an id that isn't an integer (a
    # bool is an int to Python and not to the schema), keys that aren't a
    # list of strings. And a key repeated inside one assignment collapses.
    assert refused_with("[]") is Reason.NOT_AN_OBJECT
    assert refused_with(json.dumps({"labels": []})) is Reason.WRONG_SHAPE
    assert refused_with(json.dumps({"assignments": {}})) is Reason.WRONG_SHAPE
    assert refused_with(json.dumps({"assignments": [{"answer_id": 1}]})) is Reason.WRONG_SHAPE
    assert (
        refused_with(json.dumps({"assignments": [{"answer_id": 1, "theme_keys": [], "x": 1}]}))
        is Reason.WRONG_SHAPE
    )
    assert (
        refused_with(json.dumps({"assignments": [{"answer_id": True, "theme_keys": []}]}))
        is Reason.WRONG_SHAPE
    )
    assert (
        refused_with(json.dumps({"assignments": [{"answer_id": 1, "theme_keys": "PARKING"}]}))
        is Reason.WRONG_SHAPE
    )
    assert (
        refused_with(json.dumps({"assignments": [{"answer_id": 1, "theme_keys": ["PARKING", 2]}]}))
        is Reason.WRONG_SHAPE
    )
    collapsed = json.dumps(
        {
            "assignments": [
                {"answer_id": 3, "theme_keys": ["OTHER", "PARKING", "OTHER"]},
                {"answer_id": 1, "theme_keys": []},
                {"answer_id": 2, "theme_keys": ["SAFETY"]},
            ]
        }
    )
    assert parse_assignments(Completion(text=collapsed), MAPPING) == (
        Assignment(3, ("OTHER", "PARKING")),
        Assignment(1, ()),
        Assignment(2, ("SAFETY",)),
    )


def test_a_generation_reply_is_validated_in_code() -> None:
    good = RecordingLLM().complete(GENERATION)
    themes = parse_themes(good)
    assert [theme.key for theme in themes] == ["SAFETY_1", "PARKING_1", "ACCESS_1"]
    assert all(theme.label and theme.description for theme in themes)
    assert isinstance(themes[0], ProposedTheme)

    reasons: dict[Fault, Reason] = {}
    for fault in FAULTS:
        completion = FakeLLM([fault]).complete(GENERATION)
        with pytest.raises(ReplyError) as refused:
            parse_themes(completion)
        assert refused.value.code is ErrorCode.MODEL_OUTPUT_INVALID
        reasons[fault] = refused.value.reason
    assert reasons == {
        Fault.OUT_OF_ENUM_LABEL: Reason.KEY_MALFORMED,
        Fault.DROPPED_ID: Reason.WRONG_SHAPE,
        Fault.EXTRA_ID: Reason.WRONG_SHAPE,
        Fault.DUPLICATED_ID: Reason.KEY_REPEATED,
        Fault.PROSE: Reason.NOT_JSON,
        Fault.MALFORMED_JSON: Reason.NOT_JSON,
    }
    with pytest.raises(ReplyError) as refused:
        parse_themes(Completion(text=json.dumps({"themes": []})))
    assert refused.value.reason is Reason.NO_THEMES
    with pytest.raises(ReplyError) as refused:
        parse_themes(
            Completion(
                text=json.dumps({"themes": [{"key": "A_B", "label": "", "description": "d"}]})
            )
        )
    assert refused.value.reason is Reason.WRONG_SHAPE
