# ADR-007: Rollout in three increments, parity first

## Status

Accepted, 19 September 2026.

## Context

Today's process, as described in docs/00-brief-and-data-shape.md, is a script run per open question by analysts, followed by a report and a spreadsheet prepared by hand. The target is a service a department uses without those analysts in the loop. The public evaluation says the model's part works (docs/01-research.md) and the press release says the human checking is where the hours go. The design has a lot of surface for a small team (validator, sign-off, dashboard, exports, operator console), and switching departments over in one move would take the manual path's safety net down before the new path had been shown to match it.

## Decision

Three increments. Parity comes first, and the manual script keeps running alongside until each department has matched it twice.

**Increment 1, weeks one to six, three engineers.** The plumbing and the parity gate, with no screens beyond upload and configure; warnings are resolved on the configure screen. Wrap the existing script and the themefinder library as the worker, driven stage by stage (ADR-005); the job table with lease, fence and checkpoints (ADR-002); upload by presigned S3 URL with the `stage` job and the validator with resolutions; both fan-ins and the outbox with Notify (ADR-001, ADR-006); the XLSX export. The analysts keep running it, and confirm each question's themes as-is from the operator console, which calls the same sign-off guard, because there's no sign-off screen yet. The acceptance gate: replay the piloted consultations from their archived outputs, compare the tags, and report the agreement number. That number exists before any department sees a screen.

**Increment 2.** The sign-off screen with preview and re-run (ADR-003) and the dashboard with its three predicate kinds (ADR-004), used by the pilot departments with an analyst reviewing alongside the department's own reviewer. This is where the user research is spent, because the sign-off screen is where the design is most likely to be wrong.

**Increment 3.** Self-serve for new departments behind the department table's concurrency cap and monthly budget, the report export, the operator console, a staging environment, and a load test at two concurrent 100,000-response consultations before any cross-government go-live. The manual script is switched off per department once two consultations have matched.

Sizing: three engineers, plus a designer and a user researcher part-time, for two quarters to reach increment 3, with the caveat that the sign-off screen will take more iterations than anyone plans for. The Service Standard applies to a cross-government service, so there's a service owner, research on the sign-off screen and an assessment in the plan. The service is operated to three SLOs from increment 1: themes ready within four hours for a 100,000-response consultation at p95, the email within fifteen minutes of the state change, and the dashboard filter under 500 ms at ten million answers. Week-one measurements: theme quality under partitioned generation, reviewer time per question, edits per hundred tags, cost per consultation, and the share of consultations that complete with no operator touch.

## Alternatives considered

**Build everything, then switch.** The sign-off screen is the riskiest piece and it'd be found last, and there'd be no agreement number for a department to hold onto when deciding whether to trust the tags.

**Dashboard first, because it's what people see.** A good demo. A dashboard without sign-off shows unreviewed themes to policy teams, and a dashboard without the pipeline mechanics under it can't be trusted with a 100,000-row file.

**Keep the script, add a thin interface, skip the queue.** The fastest route to a department pressing a button. Every failure mode in docs/02-architecture.md is then handled by a person, which doesn't stretch to the assumed 600 consultations a year.

**Rewrite the theming library as part of the worker.** The library is what the evaluation measured; a rewrite would need its own evaluation before it could replace anything. Wrapping keeps the evidence.

## Consequences

- An agreement number from the replay exists at the end of increment 1, from archived outputs rather than a fresh review, so it costs analyst time to read, not to produce.
- The safety net stays up until it's shown to be redundant, and that's judged per department rather than once for all of government.
- Two paths run for a while. During increments 1 and 2 analyst time is spent on both, and that's the price of the gate.
- The replay gate assumes the archived pilot outputs exist in a form that can be compared tag for tag. If they don't, the gate becomes a double review of a sample, which does cost analyst time.
- Two quarters with a part-time designer for a screen a Service Standard assessment will look at closely. The schedule allows for the sign-off screen to iterate and has no slack for anything else to.

## How I'd know this was wrong

A department that has used the service for two consultations and still runs the script, or increment 1 slipping past week six because the validator quietly grew into the configure screen.
