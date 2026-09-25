"""Multi-select cells, tokenised against the option vocabulary.

A multi-select answer is the chosen options joined by commas, and an
option can itself contain a comma (docs/00), so a cell is never split on
commas alone. The pieces between commas are matched against the
vocabulary by longest match: at each position the longest run of pieces
that spells an option wins, and a piece that spells nothing is reported
as unknown rather than guessed at (docs/02, section 3.2).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Tokenised:
    tokens: tuple[str, ...]
    unknown: tuple[str, ...]


def _pieces(text: str) -> tuple[str, ...]:
    return tuple(piece.strip() for piece in text.split(",") if piece.strip())


def tokenise(cell: str, vocabulary: Sequence[str]) -> Tokenised:
    # Both sides normalised the same way, so "a,b" and "a, b" spell the
    # same option.
    by_pieces = {_pieces(option): option for option in vocabulary}
    longest = max((len(pieces) for pieces in by_pieces), default=1)
    pieces = _pieces(cell)
    tokens: list[str] = []
    unknown: list[str] = []
    position = 0
    while position < len(pieces):
        for width in range(min(longest, len(pieces) - position), 0, -1):
            candidate = pieces[position : position + width]
            if candidate in by_pieces:
                tokens.append(by_pieces[candidate])
                position += width
                break
        else:
            unknown.append(pieces[position])
            position += 1
    return Tokenised(tuple(tokens), tuple(unknown))
