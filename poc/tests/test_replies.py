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
from consult.prompts import DATA_PREAMBLE, condense_prompt
from consult.replies import (
    MAX_DESCRIPTION,
    MAX_LABEL,
    Assignment,
    CondensedTheme,
    ProposedTheme,
    Reason,
    ReplyError,
    parse_assignments,
    parse_condensation,
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
    # list of strings, an empty list of keys. And a key repeated inside one
    # assignment collapses.
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
    unlabelled = [
        {"answer_id": 1, "theme_keys": []},
        {"answer_id": 2, "theme_keys": ["SAFETY"]},
        {"answer_id": 3, "theme_keys": []},
    ]
    assert refused_with(json.dumps({"assignments": unlabelled})) is Reason.NO_LABEL
    collapsed = json.dumps(
        {
            "assignments": [
                {"answer_id": 3, "theme_keys": ["OTHER", "PARKING", "OTHER"]},
                {"answer_id": 1, "theme_keys": ["PARKING"]},
                {"answer_id": 2, "theme_keys": ["SAFETY"]},
            ]
        }
    )
    assert parse_assignments(Completion(text=collapsed), MAPPING) == (
        Assignment(3, ("OTHER", "PARKING")),
        Assignment(1, ("PARKING",)),
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


def test_a_condensation_reply_is_validated_in_code() -> None:
    candidates = (
        ProposedTheme("SAFETY_1", "Safety (1)", "Safety raised"),
        ProposedTheme("SAFETY_51", "Safety (51)", "Safety raised"),
        ProposedTheme("PARKING_1", "Parking (1)", "Parking raised"),
    )
    prompt = condense_prompt(model_alias="fake-model", question_text="Why?", candidates=candidates)
    # The candidates go to the model as data after the data line, like answers do.
    assert DATA_PREAMBLE in prompt.system
    assert json.loads(prompt.user) == [
        {"key": c.key, "label": c.label, "description": c.description} for c in candidates
    ]
    assert prompt.answer_ids == () and prompt.theme_keys == ()
    keys = tuple(c.key for c in candidates)

    good = RecordingLLM().complete(prompt)
    condensed = parse_condensation(good, keys)
    assert sorted(condensed, key=lambda t: t.key) == [
        CondensedTheme("PARKING", "Parking", "Parking raised", ("PARKING_1",)),
        CondensedTheme("SAFETY", "Safety", "Safety raised", ("SAFETY_1", "SAFETY_51")),
    ]

    # A merge of a key nobody proposed, and one candidate folded twice.
    unknown = json.dumps(
        {
            "themes": [
                {"key": "SAFETY", "label": "S", "description": "d", "merges": ["SAFETY_1", "X"]}
            ]
        }
    )
    with pytest.raises(ReplyError) as refused:
        parse_condensation(Completion(text=unknown), keys)
    assert refused.value.reason is Reason.KEY_UNKNOWN
    twice = json.dumps(
        {
            "themes": [
                {"key": "SAFETY", "label": "S", "description": "d", "merges": ["SAFETY_1"]},
                {"key": "SAFER", "label": "S", "description": "d", "merges": ["SAFETY_1"]},
            ]
        }
    )
    with pytest.raises(ReplyError) as refused:
        parse_condensation(Completion(text=twice), keys)
    assert refused.value.reason is Reason.KEY_REPEATED
    for fault in FAULTS:
        with pytest.raises(ReplyError):
            parse_condensation(FakeLLM([fault]).complete(prompt), keys)


def themes_text(**overrides: object) -> str:
    theme: dict[str, object] = {"key": "SAFETY", "label": "Safety", "description": "Safety raised"}
    theme.update(overrides)
    return json.dumps({"themes": [theme]})


def test_a_label_or_description_is_held_to_a_line_of_plain_text() -> None:
    # Labels and descriptions come back from the model and go on to sit in
    # every later prompt and on the sign-off screen, so on the way in they
    # are one line each, of bounded length, with no control or format
    # character (a newline could start an instruction; a bidi override
    # could make the screen read backwards). Found by the security review.
    assert MAX_LABEL == 80 and MAX_DESCRIPTION == 400
    for text in ("two\nlines", "tab\tbed", "bidi\u202eflip", "zero\u200bwidth", "esc\x1b[0m"):
        with pytest.raises(ReplyError) as refused:
            parse_themes(Completion(text=themes_text(label=text)))
        assert refused.value.reason is Reason.TEXT_MALFORMED
        with pytest.raises(ReplyError) as refused:
            parse_themes(Completion(text=themes_text(description=text)))
        assert refused.value.reason is Reason.TEXT_MALFORMED
    with pytest.raises(ReplyError) as refused:
        parse_themes(Completion(text=themes_text(label="x" * (MAX_LABEL + 1))))
    assert refused.value.reason is Reason.TEXT_TOO_LONG
    with pytest.raises(ReplyError) as refused:
        parse_themes(Completion(text=themes_text(description="x" * (MAX_DESCRIPTION + 1))))
    assert refused.value.reason is Reason.TEXT_TOO_LONG
    # The condensation shape gets the same checks.
    condensed = json.dumps(
        {
            "themes": [
                {"key": "SAFETY", "label": "Safe\nty", "description": "d", "merges": ["SAFETY_1"]}
            ]
        }
    )
    with pytest.raises(ReplyError) as refused:
        parse_condensation(Completion(text=condensed), ["SAFETY_1"])
    assert refused.value.reason is Reason.TEXT_MALFORMED
    # An ordinary label and description pass, at the limit.
    ok = parse_themes(
        Completion(text=themes_text(label="l" * MAX_LABEL, description="d" * MAX_DESCRIPTION))
    )
    assert (len(ok[0].label), len(ok[0].description)) == (MAX_LABEL, MAX_DESCRIPTION)


def test_the_fallback_keys_are_reserved() -> None:
    # OTHER and NO_REASON are what sign-off adds (transitions.FALLBACK_THEMES),
    # and ON CONFLICT DO NOTHING there means a model that proposed either
    # would have its theme stand in for the fallback with is_fallback false,
    # and the preview's Other rate would be whatever the model called
    # OTHER. Refused on the way in instead. Found by the security review.
    for key in ("OTHER", "NO_REASON"):
        with pytest.raises(ReplyError) as refused:
            parse_themes(Completion(text=themes_text(key=key)))
        assert refused.value.reason is Reason.KEY_RESERVED
        condensed = json.dumps(
            {"themes": [{"key": key, "label": "L", "description": "d", "merges": ["SAFETY_1"]}]}
        )
        with pytest.raises(ReplyError) as refused:
            parse_condensation(Completion(text=condensed), ["SAFETY_1"])
        assert refused.value.reason is Reason.KEY_RESERVED


def test_a_reply_error_carries_no_chained_exception_with_the_reply_in_it() -> None:
    # json.JSONDecodeError keeps the whole document on its .doc attribute.
    # Chained as the cause, a later logger.exception or an error tracker
    # would print the reply that job.error has no column for. So the chain
    # is cut. Found by the security review.
    with pytest.raises(ReplyError) as refused:
        parse_assignments(Completion(text="Certainly! Not JSON at all."), MAPPING)
    assert refused.value.__cause__ is None
    assert refused.value.__suppress_context__ is True


def test_a_lone_surrogate_in_a_label_is_refused_by_the_reply_check() -> None:
    # A JSON escape can spell what UTF-8 can't: \ud800 decodes to a lone
    # surrogate, which the first insert then fails to encode, and would
    # be recorded as a worker error and retried rather than refused.
    # U+FFFF encodes, but XML 1.0's Char production (section 2.2) leaves
    # it out, and a label goes on to the sign-off screen and the export.
    # Both are refused on the way in as malformed text, with a reason and
    # never the text.
    for text in ("Parking\ud800lane", "Parking\uffffLane"):
        for field in ("label", "description"):
            reply = themes_text(**{field: text})
            assert text not in reply  # sent as a JSON escape, as a model would
            with pytest.raises(ReplyError) as refused:
                parse_themes(Completion(text=reply))
            assert refused.value.reason is Reason.TEXT_MALFORMED
            assert "Parking" not in str(refused.value)
