"""The cost estimate a department sees before Confirm (docs/02, step 3).

The arithmetic is docs/05 section 2, per open answer: generation reads
every answer once in batches of about 50 behind about 1k tokens of prefix
and writes about 600 tokens a call, with 5% on top for condensation and
refinement; mapping reads every answer again in batches of 10 behind a
prefix of about 2k tokens that the cache can serve, and writes about 25
tokens an answer. Prices are per million tokens, with a planning rate to
the pound. Tokens per answer and the prices are settings, because they're
the assumptions that move the number and a department should see them.

docs/05's own check: 5,000 open answers come to 2.83M tokens, £3.97 cached
and £5.09 uncached, and a test holds this module to that.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

GENERATION_BATCH = 50
GENERATION_PREFIX_TOKENS = 1_000
GENERATION_OUTPUT_TOKENS = 600
CONDENSATION_SHARE = 0.05
MAPPING_BATCH = 10
MAPPING_PREFIX_TOKENS = 2_000
MAPPING_OUTPUT_PER_ANSWER = 25


@dataclass(frozen=True)
class Rates:
    tokens_per_answer: int = 150
    usd_per_million_input: float = 2.00
    usd_per_million_cached: float = 0.50
    usd_per_million_output: float = 8.00
    gbp_per_usd: float = 0.75


DEFAULT_RATES = Rates()


@dataclass(frozen=True)
class Estimate:
    open_answers: int
    generation_calls: int
    mapping_calls: int
    tokens_uncached_input: int
    tokens_cacheable_prefix: int
    tokens_output: int
    rates: Rates

    @property
    def tokens_total(self) -> int:
        return self.tokens_uncached_input + self.tokens_cacheable_prefix + self.tokens_output

    def _pence(self, prefix_usd_per_million: float) -> int:
        rates = self.rates
        usd = (
            self.tokens_uncached_input / 1_000_000 * rates.usd_per_million_input
            + self.tokens_cacheable_prefix / 1_000_000 * prefix_usd_per_million
            + self.tokens_output / 1_000_000 * rates.usd_per_million_output
        )
        return round(usd * rates.gbp_per_usd * 100)

    @property
    def pence_cached(self) -> int:
        return self._pence(self.rates.usd_per_million_cached)

    @property
    def pence_uncached(self) -> int:
        return self._pence(self.rates.usd_per_million_input)

    @property
    def assumptions(self) -> tuple[str, ...]:
        rates = self.rates
        return (
            f"{rates.tokens_per_answer} tokens per open answer",
            f"generation reads every answer in batches of {GENERATION_BATCH} behind "
            f"{GENERATION_PREFIX_TOKENS:,} tokens of prefix, writing {GENERATION_OUTPUT_TOKENS} "
            f"a call, plus {CONDENSATION_SHARE:.0%} for condensation",
            f"mapping in batches of {MAPPING_BATCH} behind a cacheable prefix of "
            f"{MAPPING_PREFIX_TOKENS:,} tokens, writing {MAPPING_OUTPUT_PER_ANSWER} an answer",
            f"prices per million tokens: ${rates.usd_per_million_input:.2f} input, "
            f"${rates.usd_per_million_cached:.2f} cached, ${rates.usd_per_million_output:.2f} "
            f"output, at £{rates.gbp_per_usd:.2f} to the dollar (docs/05, section 1)",
        )


def estimate(open_answers: int, rates: Rates = DEFAULT_RATES) -> Estimate:
    generation_calls = math.ceil(open_answers / GENERATION_BATCH)
    mapping_calls = math.ceil(open_answers / MAPPING_BATCH)
    answer_tokens = open_answers * rates.tokens_per_answer
    generation_input = round(
        (answer_tokens + generation_calls * GENERATION_PREFIX_TOKENS) * (1 + CONDENSATION_SHARE)
    )
    generation_output = round(
        generation_calls * GENERATION_OUTPUT_TOKENS * (1 + CONDENSATION_SHARE)
    )
    return Estimate(
        open_answers=open_answers,
        generation_calls=generation_calls,
        mapping_calls=mapping_calls,
        tokens_uncached_input=generation_input + answer_tokens,
        tokens_cacheable_prefix=mapping_calls * MAPPING_PREFIX_TOKENS,
        tokens_output=generation_output + open_answers * MAPPING_OUTPUT_PER_ANSWER,
        rates=rates,
    )
