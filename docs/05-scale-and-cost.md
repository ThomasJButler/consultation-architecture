# Scale and cost

This file shows the working behind every number docs/02 quotes. Each figure points at a row of the verification log in `docs/01-research.md` (section 6) or says it's an estimate and what it rests on. Prices were checked on 19 September 2026 unless a log row says otherwise. The model arithmetic is sections 2 to 4, the platform is section 5, the people are section 6, and section 9 lists what none of it has measured.

## 1. Assumptions

Printed so anyone with a calculator can redo the sums. The first two are docs/02's own (section 1).

- Five open questions per consultation is typical; seventy exists.
- About 150 tokens per open answer. Deliberately on the high side: the Gender Recognition Act analysis averaged about 43 words an answer (docs/01, section 5, the rounded row), which is nearer 60 tokens, and it also saw single answers of 4,000 words.
- 20 answer rows per respondent once multi-select answers are exploded (ADR-004).
- Theme generation reads every answer in batches of 50 (docs/02, step 6), behind about 1k tokens of prefix per call (role, preamble, question text, follow-up template, output schema), and writes about 600 output tokens per call. Condensation and refinement add about 5% to that stage's tokens; they read candidate themes, never answers. Both figures are estimates.
- Each question also pays a fixed preview of 20 calls on 200 answers (docs/02, step 6): about 75k tokens, or 9p, per question. A re-run after edits (step 8) costs the same again. It's left out of the per-thousand line and sits inside the table's rounding.
- Mapping runs in batches of 10 (docs/02, step 9) behind a prefix of about 2k tokens, most of it a list of about 30 themes with ids and a sentence each. The prefix is identical for every call on a question, which is what makes it cacheable; Azure caches nothing under 1,024 tokens (docs/01, prompt-caching row). About 25 output tokens per answer: an id and a few enum keys.
- Prices per million tokens for gpt-4.1 in uksouth (docs/01, section 4 and the gpt-4.1 meters row): Global Standard input $2.00, cached input $0.50, output $8.00; Global Batch input $1.00 and output $4.00 with no cached meter. £0.75 to the dollar is a planning rate; the invoice will carry Microsoft's own.
- About 8 s per mapping call and about 20 s per generation call. Estimates: output tokens set the latency, and a generation call writes about twenty times more of them.
- A semaphore of 10 per job (docs/02, step 6) under dispatch caps of 4 jobs per consultation and 20 service-wide (step 4). So at most 40 calls in flight for one consultation.
- 1M tokens per minute of gateway share (docs/02, section 1), with the whole share available to the one consultation and every token counted against it, cached or not. That's the pessimistic reading of the quota; if the platform team's meter ignores cache hits the wall-clock figures improve.

## 2. Per thousand respondents

Five questions, so 5,000 open answers: 100 generation calls and 500 mapping calls.

| Stage | Uncached input | Cacheable prefix | Output |
|---|---|---|---|
| Generation, 100 calls | 750k of answers + 100k of prefix, plus 5% = 0.89M | none | 60k, plus 5% = 63k |
| Mapping, 500 calls | 750k of answers = 0.75M | 500 × 2k = 1.0M | 5,000 × 25 = 125k |
| Total | 1.64M | 1.0M | 0.19M |

The line: 1.64M × $2.00 = $3.29; the prefix at 1.0M × $0.50 = $0.50 cached, or × $2.00 = $2.00 if the cache misses; output 0.19M × $8.00 = $1.50. Cached, $5.29, which is £3.97. Uncached, $6.79, which is £5.09. Call it £4.0 to £5.1 per thousand respondents, 2.83M tokens.

Screen 1 in docs/02 (section 12) shows about £2.70 for 4,100 answers, a rate of £3.3 per thousand respondents. That figure is what a mapping prefix of about 1.3k tokens with no generation output would give; docs/02 doesn't print the assumptions behind its sketch. I've kept the longer prefix, because a preamble, a question, thirty themes with a sentence each and a schema don't fit in 1.3k, and the generation output because it exists. docs/02's figure is being aligned to this arithmetic. The prefix is the assumption that moves the number most: it's 35% of the tokens and the only part the cache discount touches. The screen will compute from the validator's own counts (docs/02, step 2), so neither sketch is what a policy team sees.

The batch lane is exactly half the uncached figure, £2.5, and no less than that, because the batch and cache discounts don't stack for gpt-4.1 (docs/01, gpt-4.1 meters row). Against the cached synchronous figure it saves about a third.

## 3. At 1k, 10k and 100k

Wall-clock is gateway time on the synchronous lane and leaves out the wait for sign-off between the two phases. Two bounds apply: tokens against the 1M TPM share, and calls in flight at the per-call latencies above, 10 per job under a cap of 4 jobs per consultation, so with five questions the fifth runs alone in a second round. The table prints whichever is longer, rounded up for stage boundaries and backoff.

| Respondents | Open answers | Model calls | Wall-clock, sync at 1M TPM | Model cost, sync (cached / uncached) | Batch lane | Database rows |
|---|---|---|---|---|---|---|
| 1,000 | 5,000 | ~700 (600 plus the fixed previews) | ~5 min to themes ready; under 10 min in all | £4.0 / £5.1 | ~£2.5 | 20k answers, ~12k tags, ~700 `job_batch` |
| 10,000 | 50,000 | ~6,100 | ~15 min to themes ready; ~45 min in all | £40 / £51 | ~£25 | 200k answers, ~125k tags, ~6k `job_batch` |
| 100,000 | 500,000 | ~60,000 | ~2.5 h to themes ready (four questions in a round of ~76 min, the fifth alone at ~67 min); 4 to 5 h of gateway time in all because map jobs for signed-off questions overlap the last generation round (about 5.5 h if nothing overlaps) | £400 / £510 | ~£255 | 2M answers, ~1.25M tags, ~60k `job_batch` |

Themes ready at 100,000 respondents is 95M tokens, about 95 minutes at the full share and about 2.5 hours once the cap of four is counted, inside the four-hour SLO (docs/02, section 7.12) with room for a takeover (ADR-002). Mapping is the larger phase: 66% of the tokens, about three hours.

At 100k TPM, a tenth of the assumed share, themes ready takes about 16 hours and the whole run about two days of gateway time. docs/02 section 7.12 calls the first a working day; two is nearer, and either way nothing meets the SLO at that share without the batch lane or a bigger allocation. The wall-clock column moves with that negotiation and with nothing in the code.

Tags are an estimate at 2.5 per open answer; the mapping prompt allows several labels per answer and I have no measured average. ADR-004's ceiling of 2.5M tags at 100,000 respondents is that rate with one reopen, since tags accumulate per version (docs/04, section 7). Two such consultations at once is the load test ADR-007 names.

What the table leaves out, each of them small. A batch that fails the two-way check retries at size 1 (docs/02, step 9): ten calls in place of one, for that batch only. A takeover re-pays one batch (ADR-002). Backoff on a 429 costs minutes, never tokens (docs/02, section 9). A reopened question maps again on a new theme-set version (ADR-004). I'd allow 5% for the lot, and the per-job ledger will say what it really was.

## 4. Cross-check against the published figure

The press release (DSIT and Defra, 16 October 2025; docs/01, section 2 and its log row) reports that Consult categorised over 50,000 responses to the Independent Water Commission's call for evidence in around two hours for £240, with 22 hours of expert checking.

Read off the table above, 50,000 respondents at five questions is £200 to £255 on the synchronous lane and about two and a half hours of gateway time at the assumed share. Same order of magnitude, same shape. A cross-check can't say more than that: the ATRS record names GPT-4o (docs/01, section 2), the prompts here are my own, that call for evidence had 73 questions with nine responses in ten arriving through campaigns (docs/01, section 5), which this design themes once and copies (docs/02, step 9), and whether a "response" counts a respondent or an answer isn't stated. If it's an answer, the comparison moves by a factor of five in this design's favour on paper, and I'd want the pilot's counting rule, model and prompt before reading anything into that.

## 5. Platform running cost, per month

Sized from docs/02, section 3. AWS prices are London list prices, all of them to re-check against the pricing pages before anyone budgets on them; none is in docs/01's log. Dollars at £0.75.

| Item | Sizing | Unit price | Source | £ a month |
|---|---|---|---|---|
| Web app, two Fargate tasks | 0.5 vCPU, 1 GB each | ~$0.05 per vCPU-hour, ~$0.0055 per GB-hour | list price, to re-check | 33 |
| Worker, one Fargate task | 1 vCPU, 4 GB (docs/02, section 3) | as above | list price, to re-check | 39 |
| RDS Postgres, db.t4g.medium Multi-AZ | 200 GB gp3 | ~$0.08 an hour single-AZ and ~$0.13 per GB-month, both doubled for Multi-AZ | list price, to re-check | 127 |
| Application load balancer | one, two AZs | ~$0.025 an hour plus about one LCU | list price, to re-check | 18 |
| Public IPv4 | three (two for the ALB, one for NAT) | $0.005 an hour each | list price, to re-check | 8 |
| NAT gateway | one, plus ~50 GB processed | ~$0.05 an hour and ~$0.05 per GB | list price, to re-check | 30 |
| Reconciler | an ECS task every five minutes (docs/02, section 3), ~1 min at 0.25 vCPU | Fargate rates above plus EventBridge Scheduler | list price, to re-check | 2 |
| CloudWatch logs and alarms | ~5 GB ingested; ids, counts and codes only (`docs/02`, section 3.4) | ~$0.59 per GB ingested; $0.10 per alarm | list price, to re-check | 4 |
| S3 | ~100 GB of uploads and exports | ~$0.024 per GB-month | list price, to re-check | 2 |
| KMS keys and secrets | two keys, a few secrets | $1 per key, $0.40 per secret | list price, to re-check | 3 |
| SQS, GOV.UK Notify | inside the free tier; Notify email is free (docs/02, section 3) | | | 0 |

About £265 a month with one worker always on, or about £225 if it scales to zero as docs/02 section 3 allows. I budget the higher one. RDS Multi-AZ is just under half, which is the price of the fan-in transaction surviving an AZ loss, and I'd pay it.

- A busy month, say two 100,000-respondent consultations and twenty ordinary ones: about £15 more of platform (worker scale-out for a day or two, NAT data, log volume, storage growth; an estimate) and £900 to £1,100 of model spend, which is the line that moves.
- A year of production: about £3.2k of platform. Model spend on top, from section 7.
- Staging: single-AZ RDS with 50 GB, one web task, the worker on demand, the same ALB, NAT and addresses: about £130 to £170 a month, £1.5k to £2k a year, less if it's stopped outside working hours.
- Not costed: the model gateway and Langfuse, which are the platform team's and shared; Sentry; and engineers' time, which ADR-007 counts in people and quarters rather than pounds.

## 6. Human time

- Sign-off (docs/02, step 8 and screen 3): reading about thirty candidates with counts and quotes and confirming as-is, 10 to 20 minutes per question. With edits and a preview re-run, 30 to 60 minutes. Estimates with no source; "reviewer time per question" is a week-one measurement in docs/02, section 8, and it replaces these.
- Calibration: a 200-answer sample at the DWP evaluation's median of 19 seconds per response (docs/01, section 2 and its log row) is 63 minutes. About an hour per question per reviewer, two hours if two reviewers produce an agreement number.
- The press release's own figure: 22 hours of expert checking against £240 of model time (docs/01, press-release row).

At 100,000 respondents the model costs £400 to £510 and five questions take about six to fifteen hours of reviewers' time (five times the 10 to 60 minutes of sign-off and the one to two hours of calibration above) before anyone opens a dashboard. Fifteen hours of reviewers' time will cost more than £510 of model spend at any plausible day rate (a department's own rate card decides by how much), and even six hours is money of the same order, so the people are the larger line, and the published 22 hours against £240 says the same thing about the pilot. That's why the sign-off screen gets the user research (ADR-007). The cost line on check-your-answers is there so nobody gets a surprise invoice, which is the guard docs/02 section 8 describes.

## 7. Government-wide

About 600 public consultations a year (DWP evaluation, docs/01, section 5 and its log row); the gov.uk search API counted 397 first published in 2025 (docs/01, section 5 and its log row). The size mix is my estimate from the shape docs/01 section 5 describes, a small median and an enormous tail: 500 at a thousand respondents or fewer, 80 at around ten thousand, 20 at around a hundred thousand. From the table:

| Band | Count | Each | Total |
|---|---|---|---|
| Up to 1k respondents | 500 | £4 to £5 | £2k to £2.5k |
| ~10k | 80 | £40 to £51 | £3.2k to £4.1k |
| ~100k | 20 | £400 to £510 | £8k to £10k |

Roughly £13k to £17k a year of model spend at the synchronous price, and a mix twice as heavy stays under £35k. The press release reports 75,000 days and £20 million a year as the manual baseline (docs/01, press-release row), reported rather than verified by me. The model spend is under a tenth of a percent of that baseline; the reviewers' hours in section 6 are what the baseline turns into, and they don't vanish. The per-department budget in pence (docs/02, section 7.10) is sized from these bands, and reconciling it against the gateway's invoice stays a monthly job for a person (docs/02, section 7, decision 10).

## 8. Bottlenecks and levers

The gateway's tokens-per-minute share sets the wall-clock at 100,000 respondents, and a reviewer's reading speed sets everything after themes ready. Both sit outside the codebase, which docs/02 section 7 says twice (decisions 6 and 12). The levers, in the order I'd pull them:

1. **The batch lane.** Half the uncached price, a 24-hour target turnaround, up to 100,000 requests per input file (docs/01, Global Batch row), so one question's 10,000 mapping calls fit one file. It moves load off the contended synchronous quota onto a different one. It suits mapping, which runs after sign-off when the wait is a reviewer's overnight; it can't rescue a four-hour themes-ready SLO. Regional deployments can't batch (docs/01, region-availability row), so a consultation pinned to UK-only inference has no batch lane and runs synchronously. First to cut in docs/02 section 11, because it's a lever and nothing depends on it.
2. **A cheaper model tier behind the same gateway.** Mapping is two thirds of the tokens and the more mechanical task. The alias per consultation (ADR-005) makes a smaller model for mapping a configuration change, gated by the evaluation on double-reviewed samples. The notes record gpt-4.1-mini with Regional Standard in UK South (docs/01, UK South model-list row, to re-check); its meter isn't in the log, so I print no price for it.
3. **A smaller mapping prefix.** At 1.2k tokens instead of 2k, still above the caching floor, the prefix drops from 1.0M to 0.6M tokens per thousand respondents: 15p saved when cached, 60p uncached, and 14% off the token demand that sets wall-clock. Condensing to about 30 themes rather than the cap of 70 (docs/02, step 6) is how it gets there. Pull this one for the wall-clock; the saving in pounds is small change.

## 9. Unmeasured

- **The filter plan on this schema.** ADR-004's query is unbenchmarked here. Consult's public ADR 0006 reports 218 ms for three closed-value predicates on ten million rows (docs/01, section 1), on its schema and its instance. PR-09's `--scale` benchmark will measure the shape of the plan: the fixture generator will build a fictional 20,000-row consultation at `--scale`, the test will load it, run ANALYZE, issue the three-predicate filter with `attrs @> '{"d_area": ["Villages"]}'` and walk `EXPLAIN (FORMAT JSON)` for a Bitmap Index Scan on the GIN index, on Postgres 17 in Docker. That proves the index is used; the 500 ms at ten million answers on db.t4g.medium stays with the staging load test (ADR-007).
- **The real TPM share.** Every wall-clock figure above scales with it. ADR-002's per-job ledger records tokens per batch and when each batch finished, so after the first real consultation the negotiation has observed rates rather than this table.
- **Token counts on real answer lengths.** 150 per answer is a planning number. The `stage` job counts tokens over the whole file before Confirm (docs/02, step 2), so the check-your-answers estimate replaces this assumption on the first upload; the model tier and the prefix length stay as printed here until measured.
- **The cache hit rate.** The cached figure assumes every mapping call after the first hits the prefix. Ten concurrent calls sharing one prefix should; `tokens_cached` on each job (ADR-005) will show whether they do, and the uncached column is the ceiling if they don't.
- Smaller ones: 8 s and 20 s per call, 2.5 tags per answer, and every AWS unit price in section 5, which is why the section says "to re-check" ten times.

PR-05 will pin the transactions the rows depend on, PR-09 will measure the one plan it can, and the rest waits for a real consultation and a real invoice.

## Correction, 19 September 2026

A review of PR-02 found fifteen inconsistencies across the design documents; `docs/07-reviews.md` logs the pass and PR-02b reconciles them. The entries below correct this file. Each names the section or sentence it corrects and gives the corrected text; the body above is left as it merged.

1. **Section 3, the method and the 100,000 row.** The table's rule is "whichever is longer of the token bound and the call bound", per round, with no overlap between the two phases. The 1,000 and 10,000 rows follow it; the 100,000 row mixed in an overlap the other rows don't assume and put the mapping phase at its token bound alone. The row by the stated method:

   | Respondents | Open answers | Model calls | Wall-clock, sync at 1M TPM | Model cost, sync (cached / uncached) | Batch lane | Database rows |
   |---|---|---|---|---|---|---|
   | 100,000 | 500,000 | ~60,000 | ~2.5 h to themes ready (four questions in a round of ~76 min, token-bound; the fifth alone at ~67 min, call-bound); ~7 h of gateway time in all | £400 / £510 | ~£255 | 2M answers, ~1.25M tags, ~60k `job_batch` |

   The mapping arithmetic behind the 7 h: 10,000 calls and 37.5M tokens per question; four questions in a round are token-bound at 150 min, the fifth alone is call-bound at 133 min (10,000 calls at 8 s, ten in flight), 283 min in all, about 4.7 hours; add the 143 minutes to themes ready. The same sums give the 10,000 row's 45 minutes (7.6 + 6.7 minutes to themes ready, 15 + 13.3 to map) and the 1,000 row's few minutes before rounding up for stage boundaries.

   The overlap assumption, stated once: the table assumes mapping starts after the last theme set is ready. If reviewers sign off questions as they arrive, map jobs run under the same cap of four alongside the last generation round and the 100,000 total is nearer 5 hours: the fifth question's solo rounds are call-bound and leave most of the share idle, which overlap fills. That figure needs a reviewer at the screen while the pipeline runs, so it isn't the one printed.

2. **Section 3, the paragraph after the table.** "Mapping is the larger phase: 66% of the tokens, about three hours" reads: about 4.7 hours by the same method, three at the token bound alone, which is why the batch lane suits it.

3. **Section 4, the 50,000 figure.** "About two and a half hours of gateway time at the assumed share" reads: about three and a half hours by the method in section 3 (72 minutes to themes ready, 142 to map), two and a half at the token bound alone. Still the same order as the reported two hours, and the rest of the paragraph stands.

4. **Section 2, the screen figure.** The paragraph beginning "Screen 1 in docs/02 (section 12) shows about £2.70" predates the alignment it describes. It reads: Screen 1 in docs/02 (section 12) shows about 2.3M tokens and about £3.30 for 4,100 answers, which is this arithmetic at the cached price, 2.83M tokens and £3.97 per 5,000 answers, scaled. The prefix is the assumption that moves the number most: it's 35% of the tokens and the only part the cache discount touches. The screen will compute from the validator's own counts (docs/02, step 2), so neither sketch is what a policy team sees.

5. **Section 3, the 100k TPM paragraph.** "docs/02 section 7.12 calls the first a working day; two is nearer" reads: docs/02 section 7.12 says the same, nearer two days than one.
