"""What the cost estimate promises: the arithmetic in docs/05 section 2,
with the assumptions printed beside the number.

docs/05's per-thousand line is the independent check: five questions and
5,000 open answers come to 2.83M tokens, £3.97 cached and £5.09 uncached.
The tokens per answer and the prices are settings, so the Confirm screen
can show a department the assumptions behind its estimate (docs/02, step 3).
"""

from __future__ import annotations

from pathlib import Path

from consult.cost import DEFAULT_RATES, Rates, estimate
from consult.definition import read_definition
from consult.responses import Responses
from consult.validate import validate

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_the_cost_estimate_follows_docs_05() -> None:
    per_thousand = estimate(5_000)

    assert per_thousand.open_answers == 5_000
    assert per_thousand.generation_calls == 100
    assert per_thousand.mapping_calls == 500
    assert per_thousand.tokens_uncached_input == 1_642_500
    assert per_thousand.tokens_cacheable_prefix == 1_000_000
    assert per_thousand.tokens_output == 188_000
    assert per_thousand.tokens_total == 2_830_500
    assert per_thousand.pence_cached == 397
    assert per_thousand.pence_uncached == 509
    assert per_thousand.rates == DEFAULT_RATES


def test_a_file_with_no_open_answers_costs_nothing() -> None:
    nothing = estimate(0)
    assert nothing.tokens_total == 0
    assert nothing.pence_cached == nothing.pence_uncached == 0
    assert nothing.generation_calls == nothing.mapping_calls == 0


def test_the_rates_are_settings_that_move_the_number() -> None:
    cheaper = Rates(
        tokens_per_answer=60,
        usd_per_million_input=1.0,
        usd_per_million_cached=0.25,
        usd_per_million_output=4.0,
        gbp_per_usd=0.8,
    )
    assert estimate(5_000, cheaper).tokens_total < estimate(5_000).tokens_total
    assert estimate(5_000, cheaper).pence_cached < estimate(5_000).pence_cached


def test_the_assumptions_are_printed_with_the_number() -> None:
    lines = estimate(5_000).assumptions
    joined = "\n".join(lines)
    assert "150 tokens" in joined
    assert "batches of 50" in joined and "batches of 10" in joined
    assert "$2.00" in joined and "$0.50" in joined and "$8.00" in joined
    assert "£0.75" in joined
    assert "docs/05" in joined


def test_the_validator_carries_the_estimate_for_its_open_answers() -> None:
    report = validate(
        read_definition(FIXTURES / "definition.xlsx"), Responses(FIXTURES / "responses.csv")
    )
    assert report.estimate.open_answers == report.open_answer_count
    assert report.estimate.pence_cached > 0
