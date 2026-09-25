"""What the multi-select tokeniser promises: a cell is matched against the
option vocabulary by longest match and never split on commas alone, because
an option can contain one (docs/00; docs/02, section 3.2)."""

from __future__ import annotations

from consult.tokenise import Tokenised, tokenise

VOCABULARY = ("Cycle", "Walk", "Run", "Wheelchair, mobility scooter or similar", "Push a pram")
# What the workbook's comma-joined `options` cell gives after a naive split:
# the comma option in two halves.
NAIVE = ("Cycle", "Walk", "Run", "Wheelchair", "mobility scooter or similar", "Push a pram")


def test_multi_select_cells_are_tokenised_by_longest_match() -> None:
    cell = "Cycle, Wheelchair, mobility scooter or similar"
    assert tokenise(cell, VOCABULARY) == Tokenised(
        ("Cycle", "Wheelchair, mobility scooter or similar"), ()
    )
    assert tokenise("Wheelchair, mobility scooter or similar, Walk", VOCABULARY).tokens == (
        "Wheelchair, mobility scooter or similar",
        "Walk",
    )
    # Against the naive vocabulary the same cell is three tokens, all known,
    # which is exactly the case the validator's never-apart warning catches.
    assert tokenise(cell, NAIVE).tokens == ("Cycle", "Wheelchair", "mobility scooter or similar")


def test_spacing_around_commas_does_not_matter() -> None:
    assert tokenise("Cycle,Walk", VOCABULARY).tokens == ("Cycle", "Walk")
    assert tokenise(" Cycle ,  Walk ", VOCABULARY).tokens == ("Cycle", "Walk")
    assert tokenise("Wheelchair,mobility scooter or similar", VOCABULARY).tokens == (
        "Wheelchair, mobility scooter or similar",
    )


def test_a_token_outside_the_vocabulary_is_reported_not_guessed() -> None:
    assert tokenise("Cycle, Skateboard", VOCABULARY) == Tokenised(("Cycle",), ("Skateboard",))
    assert tokenise("cycle", VOCABULARY) == Tokenised((), ("cycle",))
    assert tokenise("", VOCABULARY) == Tokenised((), ())
    assert tokenise("Cycle, , Walk", VOCABULARY).tokens == ("Cycle", "Walk")
