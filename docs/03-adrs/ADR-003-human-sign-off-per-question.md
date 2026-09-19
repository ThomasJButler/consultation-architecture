# ADR-003: A named person signs off the themes, per question

## Status

Accepted, 19 September 2026.

## Context

The published evaluations (i.AI; URLs and retrieval dates in docs/01-research.md, section 2) report agreement with human reviewers in the high 0.7s by F1, above the figure between two human groups, with reviewers correcting roughly one mapping in four in the DWP report of 27 August 2025. The press release for the water commission consultation (DSIT and Defra, 16 October 2025; same log) puts the model's run at around two hours and the expert checking at around 22 hours. Read together: the model's themes are good and not final, and human time is the cost that matters. A service every department relies on also needs someone accountable for the theme list that ends up in a published report, and that person is on the policy team, not on the platform team.

## Decision

`signed_off` is a mandatory state on every open question, between `themes_ready` and mapping. Nothing is tagged until a named person has confirmed the list for that question. There is no auto-approve in the first version.

The screen shows the candidate themes ranked by how often they appeared in a preview mapping of a stratified sample of 200 answers, with quotes, the longlist the condensing step folded in, and the preview's Other rate. The reviewer can rename, merge, split, add and remove; each edit carries a version guard so two reviewers on one question get a conflict message rather than a silent overwrite. "Re-run preview" enqueues a `preview_themes` job (about twenty calls on the same sample) so the counts reflect the edited list; until then they're labelled as coming from the AI's original list. "Confirm as-is" is one click, because most questions won't need edits and the screen shouldn't punish that.

Confirm is a guarded update and the guard is the mutex:

```sql
UPDATE question SET status = 'signed_off'
 WHERE id = $q AND status = 'themes_ready';
```

Zero rows means someone else confirmed first. In the same transaction: freeze `theme_set_version` v2 (status `signed_off`, parent v1) with stable keys plus the `OTHER` and `NO_REASON` fallbacks, write an audit event with the reviewer's id, and insert a `map_themes` job for this question only. The web app calls the guard; it doesn't implement it.

Around the gate: `question.assigned_to` and `review_started_at` show on the task list, a reminder email goes out after five working days in `awaiting_review` (ADR-006), and time spent in that state is the one product KPI on the overview. Reviewers are the department's policy team; the analysts who ran the pilots support a department's first two consultations (ADR-007). Every human edit, here and on individual tags later, is kept with `source = 'human'` and a user id (ADR-004), and the manifest export reports an agreement rate from them.

## Alternatives considered

**Auto-approve above a confidence threshold.** There's no signal on a proposed theme list that predicts what a reviewer will add, and the one-in-four figure from the DWP evaluation is exactly the residual a threshold would skip. It could be earned later, per department, after a run of consultations with a low edit rate. Not now.

**Sign off the whole consultation at once.** One screen instead of seventy. But a reviewer with seventy questions then blocks mapping on all of them until the last is read, and questions are how policy teams already divide the work between people. Per question, a finished question maps and appears on the dashboard while its siblings are still being read.

**Review after mapping, tags rather than themes.** The reviewer sees real counts, which is nicer. Any change to the list means mapping again, and mapping is most of the model spend (docs/05-scale-and-cost.md). A 200-answer preview gives the counts for the price of twenty calls.

**Humans write the themes; the model only maps.** Throws away the part the evaluation shows working. The longlist and the preview give the reviewer the material without the reading.

## Consequences

- A named person and a timestamp on every theme list. That's the line a department's report can cite when a member of the public asks who decided.
- The edit stream is evaluation data for free, per consultation, and it's the gate for any model change (ADR-005).
- The sign-off screen is the hardest interface in the service and will take several rounds of user research (ADR-007). I've budgeted for that and I still expect to be wrong about how many.
- A consultation can sit in `awaiting_review` for weeks. The pipeline is built for that (nothing runs, nothing is held open), and the reminder and the KPI are the product's answer, not the pipeline's.
- A reviewer who clicks confirm-as-is on everything turns the gate into a rubber stamp. The edit rate per reviewer, the time in review and the preview Other rate make that visible on the overview. They don't prevent it, and I don't think software should.

## How I'd know this was wrong

A department whose edit rate sits at zero across consultations while its preview Other rate stays high, or reviewer time per question so large that policy teams drift back to the spreadsheet.
