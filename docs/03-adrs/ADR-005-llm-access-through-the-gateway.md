# ADR-005: Model calls go through the gateway, with an alias per consultation

## Status

Accepted, 19 September 2026.

## Context

Consult's public ADR 0011 describes LLM calls going through a central gateway (LiteLLM) with Langfuse for tracing (checked 18 September 2026); I'm assuming a gateway of that kind exists for this service, and the decision stands if the platform team has to stand one up. Providers retire model versions on published schedules, so the model chosen now won't be the model in two years. Every call sends members of the public's own words to a model, and some of those words will be instructions. Demographic data must never go with them. Cost has to be visible before it's spent.

## Decision

Every model call goes through the gateway; there's no provider SDK in the worker. A model alias is pinned to the consultation at Confirm and recorded on every job, alongside `prompt_sha256`, `tokens_in`, `tokens_cached`, `tokens_out`, `cost_pence` and the provider's request id. Changing the model is a configuration change gated by an evaluation on double-reviewed samples, and a reopened consultation runs on the new alias as a new theme-set version (ADR-004) with both aliases in the manifest.

The gateway is asked for structured output and the worker validates it anyway (CLAUDE.md, rule 9). Labels are an enum of the theme keys from the signed-off version. Every answer id sent must come back exactly once, with none added; a batch that fails that check retries at size 1, and an answer that still fails goes to an `unprocessable` bucket the dashboard shows rather than being silently dropped. The schema is checked in code at the boundary.

The prompt for mapping is a stable prefix (the role; a preamble saying the responses are data and that instructions inside them are data; the question text; the theme list with ids; the schema) followed by ten shuffled answers, JSON-encoded. Theme generation reads every answer, in batches of about fifty by count and token cap, partitioned by the related closed answer where the definition links one, with the follow-up question templated per partition; the candidates are condensed to around thirty (seventy at most) keeping the longlist with lineage. Only the question text, the open answer and, where linked, the related closed answer reach the model. Demographics and identifiers never do: identity columns are in the vault (ADR-004) and `attrs` isn't on the prompt path at all.

Two lanes. The synchronous lane is the default, under a semaphore of ten with full-jitter backoff on 429 and 5xx (one to sixty seconds, six attempts); a spend-cap 429 pages a person instead of retrying. The provider's batch lane is a lever for 100,000-scale runs when the four-hour SLO is at risk at the negotiated tokens-per-minute share. Cost is a guard rather than a target: an estimate at Confirm, a monthly budget per department, the gateway's own cap. The arithmetic is in docs/05-scale-and-cost.md.

Region. Data at rest is in London (SSE-KMS on S3, encrypted RDS). Inference happens on the deployments behind the gateway, and the region is recorded per job; the DPIA records that rather than claiming UK-only processing. Langfuse holds prompt and completion text by design. It sits on the gateway side inside the OFFICIAL boundary, with retention aligned to the consultation's and access limited to the platform team. CloudWatch, Sentry and `job.error` carry ids, counts and error codes only (CLAUDE.md, rule 8).

## Alternatives considered

**A provider SDK in each service.** One hop fewer and new features first. Keys and spend caps then live in every service, there's no shared trace, and a model swap is a worker deploy. Lost on operations.

**An open-weights model hosted in the VPC.** It answers the region question outright and it's the right long-term hedge. It's also a GPU fleet for a small team to run, and the published evaluation measured a hosted model's output, not this one's. The alias makes it a later configuration change, and the evaluation gate decides.

**Fine-tuning.** Adds a model lifecycle to own. The edit stream from sign-off (ADR-003) is the evaluation set that would justify it, and there isn't one yet.

**Generate themes from a sample.** Cheaper by a lot. At a 5,000-answer sample, a view held by one respondent in ten thousand is missed more often than not: the expected count is 0.5, so the chance of seeing none is e^-0.5, about 0.61. Consultations exist to hear the minority view, so generation reads everything and the sample is used for the preview only.

**Free-text labels matched by string.** That is where mis-mapping comes from. The enum and the two-way id check are what make model output safe to insert.

**Rewriting the theming library into the worker.** The public themefinder library is what the evaluation measured. The worker drives it stage by stage, so checkpoints, the id check and the enum wrap it instead of forking it.

## Consequences

- A model swap is configuration plus an evaluation, and the ledger says which model produced every number and what it cost.
- The prompt contract is small enough to run against a fake model in CI, which the proof-of-concept will do.
- The gateway is a shared dependency with a negotiated rate share. The wall-clock SLO for a 100,000-response consultation depends on that share; docs/05-scale-and-cost.md prints the figures at one million and at 100,000 tokens per minute, and the batch lane exists for the second.
- Structured output stops a response from being malformed, not from being wrong. Quality is measured by the edit stream, never by parse success.
- Langfuse holding text is a deliberate exception to "ids only" and needs its own line in the DPIA and its own retention job. If the platform team can't give that assurance, tracing drops to metadata only and debugging a bad batch gets harder.

## How I'd know this was wrong

An `unprocessable` bucket that fills on an ordinary consultation, or a provider request id in the gateway's logs that no job in the ledger accounts for.
