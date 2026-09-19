# Architecture

**Status:** Design frozen at PR-01. **Owner:** Thomas Butler. **Date:** 19 September 2026.

The master reference for the service: what I've assumed, what I've decided and why, how one consultation moves through the pipeline, and what the five screens look like. `docs/00-brief-and-data-shape.md` says what the service is for and what the input files look like. `docs/01-research.md` holds every source with its retrieval date. `docs/03-adrs/` records the decisions in section 7 one by one. The data model, the cost arithmetic and the security notes get their own files in PR-02 (`docs/04`, `docs/05`, `docs/06`).

Read it top to bottom once. The part you'll come back to is section 4, the pipeline, and the SQL in it.

## 1. Assumptions

Printed so the numbers can be checked and the design argued with.

| Assumption | Why it matters |
|---|---|
| About 600 public consultations a year across government (DWP Pathways to Work evaluation report, 27 August 2025; URL and retrieval date in `docs/01`) | Sets the idle-to-busy ratio and the case for one shared service |
| The largest consultations exceed 100,000 responses and are campaign-heavy | Sizes the ingest path, the dashboard indexes and the duplicate handling |
| Five open questions is typical; seventy exists | Per-question jobs and per-question sign-off have to work at both ends |
| About 150 tokens per open answer | Drives the cost estimate shown before any spend |
| Policy teams aren't technical | Configure in the app, with check-your-answers before Confirm |
| Departments are data controllers; the platform is a processor | Department scoping on every table; the manifest export pre-fills their DPIA |
| Authentication is solved upstream; every request arrives with a user and a department | Auth is out of scope here. Authorisation, meaning scoping, is not |
| A consultation is analysed once, then corrected; never silently re-run after publication | Immutable theme-set versions; a correction is a new version with an audit trail |
| The model gateway's throughput share is negotiated with the platform team; 1M tokens per minute assumed for the wall-clock figures | The 100,000-response wall-clock estimate scales with this number, not with the code |

## 2. Thesis

Judgement first. Three calls shape everything else.

1. The AI proposes themes and a named person signs them off, per question, before any response is tagged. Every edit that person makes is kept as evaluation data. The tags are only useful if the policy team will stand behind them.
2. The upload is validated and costed before a single model call, and demographics never enter a prompt.
3. One boring platform: one Postgres, one queue, one container image run as web, worker and reconciler, the team's existing model gateway, GOV.UK Notify. A small team can run it for every department, and fairness between departments is enforced by the platform rather than by asking nicely.

Then the mechanism. Every state transition is a database transaction. A worker finishing a question locks the consultation row, writes its result, updates the question's state, checks whether every open question has reached this milestone, flips the consultation if so, and inserts a notification row. One commit. That transaction is the human gate, the fan-in and the email trigger. No workflow engine, no counters, no Redis.

I read the public Consult repository and its ADRs before designing (`github.com/i-dot-ai/consult`, read 18 September 2026; the list is in `docs/01`). ADR 0007 and ADR 0008 there chose one SQS queue, a dedicated worker, Postgres as the single source of truth and one job per open question. I'd reasoned my way to the same shape before reading them, which I took as a good sign, and I've kept it because the reasoning holds. Three things I make explicit that those documents leave open: the fan-in, effectively-once email, and the sign-off guard enforced server-side.

## 3. Components

| Component | Technology | Responsibility, and the one-line reason |
|---|---|---|
| Web app (forms) | Django views, GOV.UK Frontend; ECS Fargate behind an ALB | Create, upload, validate, configure, sign-off, exports. Progressive enhancement; works without JavaScript. Django because it's the stack in the public Consult repository, and the admin, ORM and migrations come free. |
| Dashboard | One Svelte + TypeScript island over a JSON API | Filter state lives in the URL query string so a view can be shared and the back button works. GOV.UK Design System components. Page numbers with OFFSET on the dashboard; keyset cursors for export and deep scroll. |
| Validator | A module in the web app, run inside the `stage` job | Every check that can fail before spend (3.2). Errors block; warnings carry a resolution. |
| PostgreSQL | RDS Postgres 17, Multi-AZ, starting at db.t4g.medium | Every fact and every piece of pipeline state. GIN `jsonb_path_ops` on `respondent.attrs`; B-tree `(question_id, id)` on `answer`; a partial index on live tags; tsvector + GIN on open answers. No pgvector, no materialised views in the MVP. |
| S3 | eu-west-2, SSE-KMS, Block Public Access, TLS only | Raw uploads keyed by sha256; generated exports. Nothing the pipeline depends on after ingest: the `stage` job reads the upload from it once, and exports are served from it. |
| SQS + DLQ | Standard queue, at-least-once; `maxReceiveCount` 10 | One message per job, sent by whichever transaction queues the job, with the reconciler as the slow path. Messages are hints; the `job` table is authoritative. The DLQ catches a message nobody expected; `job.attempts` is the retry budget. |
| Worker | Same image, separate ECS service, 1 vCPU / 4 GB; autoscale on backlog from zero; scale-in protection while a job runs; `stopTimeout` 120 s | Claim with lease takeover and a fencing token; heartbeat on a background thread; checkpoint per batch; results, state change, fan-in and outbox row in one transaction. |
| Reconciler | Same image; EventBridge Scheduler runs it as an ECS task every five minutes | Five idempotent statements (section 5). Exists so nothing depends on a worker being alive. |
| Model gateway | The team's central gateway (ADR 0011 in the Consult repo, August 2026, chose LiteLLM with Langfuse and pydantic-evals for evaluation) | Model alias pinned per consultation and recorded on every job; structured outputs; keys, spend caps and traces live there. |
| Outbox + GOV.UK Notify | `notification_outbox` table; relay with `FOR UPDATE SKIP LOCKED`; Notify reference = outbox id | One email per consultation milestone: name, status and a link. Email through Notify is free (Notify pricing page, checked 19 September 2026). |
| Operator console | Django admin over `job`, `question`, `notification_outbox`, `department` | Retry means "set status to pending". Every action writes an `audit_event`. |
| Observability | CloudWatch (ERROR, ids only); Langfuse via the gateway; Sentry with `send_default_pii=False` and a scrubber; Terraform | Correlation ids on every line; per-job tokens and cost; alarms on DLQ depth, job age and outbox age. |

### 3.1 The frontend decision

Forms are server-rendered Django views with GOV.UK Frontend, because create, configure and sign-off are forms and the Design System already has the patterns they need: check your answers, error summary, task list. The dashboard is the one place that needs client-side state, so it's one Svelte + TypeScript island over the JSON API. ADR 0004 in the Consult repo chose to assume JavaScript on the dashboard, filters as a query string and server-side pagination. I've kept all three.

Pagination: page numbers with OFFSET on the dashboard, because that's what the GOV.UK Pagination component renders and nobody reads past page 200. Export and deep scroll use a keyset cursor on `(question_id, id)`.

Accessibility is assessed on the service, not on the component library. WCAG 2.2 AA here means filter changes announced to screen readers and a non-visual path through large tables, tested with people who use assistive technology.

I write FastAPI day to day. I chose Django for team fit on a cross-government tool, and I'd keep the SQL in section 4 as raw SQL inside `transaction.atomic()` so it stays visible.

### 3.2 Validator defaults

Run inside the `stage` job over the whole file, never inside a request.

| Check | Outcome |
|---|---|
| A column the definition references is missing from the responses file | Error |
| `response_type` not one of single-select, likert-5, multi-select | Error |
| `related_closed_column` names something that isn't a closed question | Error |
| `related_closed_column` is `-` or blank | No related question. Not an error |
| `-` or blank in any cell | Not answered, for every kind of question |
| `N/A` | A real facet value on demographic and closed questions; not answered on open questions; overridable per question at configure time |
| A closed value outside the option list | Warning with a resolution: map it to an option, add it as an option, or treat it as not answered |
| Multi-select cells | Tokenised against the option vocabulary, never split on commas, because an option can contain one |
| A header in the responses file that no definition sheet mentions | Gets a role: respondent id (the default for a column named like an id), identity (goes to the vault), or ignore |
| Demographic columns | Distinct observed values listed with counts, so spelling and case variants surface before spend |

A synthetic "Not answered" facet compiles to `NOT (attrs ? '<column>')`, so "people who skipped the commute question" is a filter like any other. The token estimate and the cost in pounds sit on the check-your-answers page above the Confirm button.

### 3.3 The operator console

Django admin over four tables is the whole console in the MVP. An operator can retry a failed job (status back to `pending`; the reconciler dispatches it), reassign a question, pause a department, and resend an outbox row. Every action writes an `audit_event` with the actor and the before and after. There's no bespoke UI for this until the admin proves insufficient.

### 3.4 What holds response text

Langfuse holds prompt and completion text. That's its job, and it means response text leaves the database. It sits inside the OFFICIAL boundary on the gateway side, retention is aligned to the consultation's `retention_until`, and access is limited to the platform team. Everything else (CloudWatch, Sentry, emails, `job.error`) carries ids, counts, durations and error codes only. `job.error` stores an error code and a provider request id, never a message body. A test will pin that.

## 4. The pipeline

Diagram 2 in `submission/` is the per-question state machine. Here it is in full, twelve steps and one half-step.

### Step 1. Create and upload

Name, source (Citizen Space, Qualtrics or generic), the responses file as CSV or XLSX, and optionally the definition workbook to pre-fill step 3. The browser uploads straight to S3 with a presigned multipart URL. The web app records the key and the sha256 and reads only the header row synchronously.

### Step 2. Stage and validate (a job; no spend)

Confirming the headers inserts a `stage` job. The worker COPYs the file into a staging table and writes the validation report: missing headers, unknown response types, unresolved follow-up links, unknown closed values with counts and example rows, multi-select tokens outside the vocabulary, unmatched headers, row and token counts, the cost estimate. A 100,000-row file can't be parsed inside an ALB request and the validator needs a full pass, so this is a job like any other. Consultation `staging → staged`.

### Step 3. Configure in the app

The configure screen reads the report. Each column gets a kind: demographic; closed, with its type and options; open, with an optional follow-up link; identity; respondent id; ignore. Each warning gets a resolution. Then check-your-answers with the token estimate and the cost. Confirm inserts the `ingest` job and returns 202.

The definition workbook is an importer that pre-fills this screen. It isn't the configuration. Its `options` column is comma-joined, so an option containing a comma can't be expressed in it, and that alone is reason enough for the configuration to live in the app.

### Step 3a. Ingest (a job)

The worker streams rows with COPY and explodes multi-select answers to one `answer` row per chosen option (ADR 0006 in the Consult repo made the same choice). It builds `respondent.attrs` with every demographic and closed answer keyed by column, values always arrays, so containment works the same for single- and multi-select. Identity columns go to the `vault` schema through an insert-only role. It computes `answer.duplicate_of_answer_id` (same question, identical normalised text) and `respondent.duplicate_of` (every open answer identical: a campaign proforma), runs ANALYZE, inserts one `find_themes` job per open question and sets the consultation `processing`. One transaction.

### Step 4. Dispatch

The request or job that inserts a job row dispatches it in the same breath: `pending → queued` under the caps, commit with `sent_at`, then send to SQS. The reconciler repeats the same statement every five minutes as the slow path, for crashes and for slots the caps have just freed, so a reviewer who clicks Re-run preview isn't waiting on a schedule. Commit first, because a message for a job the database doesn't know is queued is a message the claim will refuse. Caps: 6 jobs per department, 4 per consultation, 20 service-wide, round-robin across departments when contended. A department past its monthly budget is paused with an attention outbox row.

### Step 5. Claim, with a fence

```sql
UPDATE job
   SET status = 'running', attempts = attempts + 1,
       claimed_by = $worker, heartbeat_at = now()
 WHERE id = $job
   AND (status = 'queued'
        OR (status = 'running'
            AND heartbeat_at < now() - interval '10 minutes'))
RETURNING attempts;
```

The returned `attempts` is the fencing token. Zero rows means the lease is live, or the job is already done or unknown; the worker logs which and deletes the message.

Every later write starts with the heartbeat:

```sql
UPDATE job SET heartbeat_at = now()
 WHERE id = $job AND claimed_by = $worker
   AND attempts = $fence AND status = 'running';
```

Zero rows means someone else holds the lease now. The worker aborts at the next batch boundary and writes nothing more. A zombie that wakes after takeover can't write, because its fence is stale.

### Step 6. Find themes

Shuffle with a stored seed. Batch about 50 distinct answers by count and token cap (exact duplicates were flagged at ingest and are themed once), partitioned by the related closed answer where the question has one, with the follow-up question's placeholder filled from that answer. Generate candidates with structured outputs under a semaphore of 10. Condense to about 30 (cap 70), keeping the longlist with lineage back to the candidates. Refine. Preview-map a stratified sample of 200 answers so each candidate gets a count and quotes. Checkpoint every batch into `job_batch` with `ON CONFLICT DO NOTHING`. Then write `theme_set_version` v1 (`UNIQUE (question_id, version_no)`) and set the question `themes_ready` in the same transaction as fan-in 1.

The worker drives themefinder stage by stage rather than calling its top-level function. One `job_batch` is one call of one stage function on one chunk. That's how checkpoints, the two-way id check and enum labels wrap the library instead of forking it. Rewriting a library the team maintains would be the wrong first move.

### Step 7. Fan-in 1: awaiting review

Same transaction as the end of step 6.

```sql
BEGIN;
SELECT id FROM consultation WHERE id = $c FOR UPDATE;
UPDATE job SET heartbeat_at = now()
 WHERE id = $job AND claimed_by = $worker AND attempts = $fence AND status = 'running';
-- zero rows: ROLLBACK, the lease is gone

-- theme_set_version v1 and its themes are inserted here

UPDATE question SET status = 'themes_ready'
 WHERE id = $q AND status = 'finding_themes';

UPDATE consultation SET status = 'awaiting_review'
 WHERE id = $c AND status = 'processing'
   AND NOT EXISTS (
         SELECT 1 FROM question
          WHERE consultation_id = $c AND kind = 'open'
            AND status IN ('configured', 'finding_themes'));

-- only if the UPDATE above touched one row:
INSERT INTO notification_outbox (consultation_id, kind)
VALUES ($c, 'themes_ready')
ON CONFLICT DO NOTHING;
COMMIT;
```

Why the lock comes first, in its own statement. Under READ COMMITTED each statement sees a snapshot as of the instant it begins, and an UPDATE that blocks on a concurrently updated row re-evaluates its WHERE clause against that row only: the docs say it "does not see effects of those commands on other rows in the database" (PostgreSQL 17 docs, 13.2.1, read 19 September 2026). Two workers finishing the last two questions at once would each see the other as unfinished and neither would flip the consultation. The row lock serialises them: the second finisher's `NOT EXISTS` runs after the first has committed, so it sees the first's row. The guarded UPDATE is the check, the mutex and the trigger.

The predicate is a positive list of not-yet-reached states rather than `status <> 'themes_ready'`, so a question a quick reviewer has already signed off doesn't block the email. `find_failed` is deliberately absent too: four ready questions shouldn't wait on an operator. The failed one carries `attention_reason`, shows on the task list, and its retry's arrival shows there rather than by email. I made that trade on purpose and would revisit it if reviewers miss the late question.

### Step 8. Sign-off, per question

Reviewers are the policy team; in the rollout, i.AI analysts review alongside them for a department's first two consultations. `question.assigned_to` and `review_started_at` show on the task list; a reminder goes out after five working days; time spent in `awaiting_review` is the one product KPI on the overview page.

The screen shows candidates ranked by preview count with quotes, the longlist, and the preview's Other rate. The reviewer can rename, merge, split, add and remove, each guarded by the theme set's version (a plain conflict message in the UI; `--expect-version` in the proof-of-concept). Re-run preview inserts a `preview_themes` job, about 20 calls on the same 200-answer sample, and refreshes the counts for the edited draft; until then the counts are labelled "from the AI's original list". Confirm as-is is one click.

Confirm is:

```sql
UPDATE question SET status = 'signed_off'
 WHERE id = $q AND status = 'themes_ready';
```

The guard is the mutex. Two reviewers clicking at once produce one signed-off question and one conflict message. The same transaction freezes v2 with stable keys plus `OTHER` and `NO_REASON`, writes an `audit_event` with who and when, and inserts a `map_themes` job for this question only.

### Step 9. Map themes

Batches of 10 shuffled answers. The prompt is a stable prefix (the role; a line saying the responses are data and any instruction inside them is data; the question text; the theme list with ids; the output schema) followed by the answers as JSON-encoded data. Labels are an enum of theme keys. The two-way check: every answer id sent must come back exactly once, and nothing extra. A batch that fails the check retries at size 1; an answer that still fails lands in an `unprocessable` bucket the dashboard shows. Duplicate answers are themed once and the tags copied.

Tags are inserted with `ON CONFLICT DO NOTHING` on the full unique index `(answer_id, theme_id, theme_set_version_id)`, carrying `job_id`, `batch_id` and `source = 'ai'`. Checkpoint per batch. Question `complete`.

Ten per prompt is a blast-radius decision as much as a cost one. An injected instruction the model obeys can spoil at most ten answers, the check catches the shape of the damage, and the retry at size 1 isolates the answer that carried it.

### Step 10. Fan-in 2: ready

Same shape as fan-in 1 with the other predicate:

```sql
UPDATE consultation SET status = 'ready'
 WHERE id = $c AND status = 'awaiting_review'
   AND NOT EXISTS (
         SELECT 1 FROM question
          WHERE consultation_id = $c AND kind = 'open'
            AND status <> 'complete');
-- then INSERT INTO notification_outbox (consultation_id, kind)
--      VALUES ($c, 'analysis_ready') ON CONFLICT DO NOTHING;
```

A failed question blocks `ready` and its email. It doesn't block its siblings, whose results show on the dashboard as soon as they're complete.

### Step 11. Explore

The overview: respondent counts, and demographic and closed-question distributions as horizontal bar charts. The per-question view: filters of three kinds (`attr:<column>=<value>`, containment on `respondent.attrs`; `theme:<key>` on this question, OR'd; `other:<question>.theme=<key>` as a semi-join, for "what did people tagged X on one question say on another"); the theme table with its denominator stated ("of respondents who answered this question"); the related closed question's distribution; response cards with AI and human badges and a tag edit.

Retracting a tag sets `retracted_at`; re-adding clears it; the audit event is the history. Tags are never deleted. The full unique index is what stops a resumed AI run from re-inserting a tag a human has retracted. A per-response correction with an audit trail exists for "my response was mis-themed", and a published methodology note says what the service does and doesn't do.

### Step 12. Export

Two kinds. XLSX: the original columns, one column per theme, a per-question summary sheet and a manifest sheet (model alias, prompt hash, theme-set versions, run ids, the agreement rate derived from human edits, exclusions, retention date). And a report, as a print view or DOCX: per-question theme table and chart, closed-question distributions, the related-closed cross-tab, the manifest. The report exists so the policy team stops hand-building one.

## 5. The reconciler's five statements

Every five minutes, in order, each idempotent.

1. Dispatch: `pending → queued` under the caps, commit, then send. The slow path: the transaction that inserts a job does this itself first.
2. Recover: `queued` with `sent_at` older than ten minutes, or `running` with a stale heartbeat. If `attempts >= 5`, mark the job `failed`, set the question to `find_failed` or `map_failed`, set `attention_reason` and insert an attention outbox row. Otherwise re-send. Duplicates are harmless because the claim is conditional.
3. Retry: `failed_retryable` whose `next_attempt_at` has passed and `attempts < 5` goes back to `pending`.
4. Re-run both fan-in predicates for any consultation whose questions have all reached a milestone but whose status hasn't advanced. The outbox row shares the transition's commit, so a crash can't lose it. This statement is for the case where statement 2 has just moved the last unfinished question to `find_failed`: no worker transaction runs then, and fan-in 1 doesn't wait on a failed question, so this is what flips the consultation to `awaiting_review` and queues the email. (`map_failed` blocks fan-in 2 by design, so that case waits for the retry.)
5. Relay unsent outbox rows.

## 6. Consultation-level states

Question states are `configured → finding_themes → themes_ready → signed_off → assigning_themes → complete`, with `find_failed` and `map_failed` and their retry edges (Diagram 2). A reopened question goes `complete → themes_ready` with a new candidate `theme_set_version` (parent = the signed-off one); its old tags stay on the old version. Diagram 2 leaves that edge out to stay inside its word budget. The consultation's state is derived from them.

| From | To | Trigger | Written by | Email |
|---|---|---|---|---|
| `draft` | `staging` | Headers confirmed; `stage` job inserted | Web app | No |
| `staging` | `staged` | `stage` job writes the validation report | Worker, final transaction of the job | No |
| `staged` | `processing` | `ingest` job finishes; `find_themes` jobs inserted | Worker, one transaction | No |
| `processing` | `awaiting_review` | Fan-in 1: no open question still in `configured` or `finding_themes` | Worker, or reconciler statement 4 | Themes ready |
| `awaiting_review` | `ready` | Fan-in 2: every open question `complete` | Worker, or reconciler statement 4 | Analysis ready |
| `ready` | `awaiting_review` | A question reopened for correction, or re-run on a new model alias; a new theme-set version, and its later `ready` email carries that version as `subject_id` so it cannot collide with the first | Web app, on a reviewer's action, through `advance_consultation` | No |
| any | same state | A job reaches `failed`: `attention_reason` set (`stage_failed`, `find_failed:<question>`, `map_failed:<question>`, `budget_exceeded`) | Reconciler statement 2, or dispatch | Attention needed |
| any | same state | Operator retry: job back to `pending`, `attention_reason` cleared | Operator console | No |

Every path that can move a consultation's state goes through one routine, `advance_consultation(id)`: take the row lock, run both guarded UPDATEs, insert the outbox row if one of them fired. The worker's completing transaction calls it, so do reconciler statements 2 and 4, a reopen, and an operator retry. There is no second way to change the column, which is what stops a reopen racing a worker's fan-in.

`attention_reason` is nullable and orthogonal to the state; the task list shows it as a banner. There's no `failed` state for a consultation because nothing about a consultation fails. A job does, and the consultation waits.

## 7. Key decisions

Each with the alternatives I weighed, the trade-off, and the weak point I'd own in a review. `docs/03-adrs/` expands the ones that need a full record.

### 1. Orchestration in the database, not an engine

Alternatives: AWS Step Functions (the strongest; pennies per run with one Task state per job, about a pound per 50,000-response run only if every batch were a state transition, at the list price in ADR-001; a visual audit trail; redrive); the RQ, Batch, EventBridge and Lambda pipeline that ADR 0007 describes replacing; Celery or RQ; Temporal. I chose Postgres because local runnability and a single queue are values ADR 0007 itself names, because the fan-in is one statement you can show on a whiteboard, and because nothing sits parked for a fortnight across deploys. Trade-off: no engine UI, so the operator console has to exist from day one. Weak point: if a second workflow shape appears, or fan-out needs thousands of child executions, the case for Step Functions comes back and I'd revisit.

### 2. Completion detection inside the completing transaction

`FOR UPDATE` on the consultation row, then the guarded UPDATE, plus the reconciler's fourth statement. Alternatives: a `questions_remaining` counter (a lost update under concurrency unless it's also locked, and then you've reinvented this); a separate completion-check job (double-fire unless it's idempotent, and a delay either way); polling from the web app. Weak point: the pattern depends on every transition path taking the lock, and a code path that forgets it is silent until two workers finish together. The proof-of-concept (PR-05) will pin this with twenty threaded iterations and a hand-stepped two-connection reproduction without the lock, so the failure is on record before the fix is.

### 3. One job per (consultation, open question, kind, run)

With lease takeover, a fencing token, `job_batch` checkpoints, caps applied at dispatch, and SIGTERM draining inside `stopTimeout`. Alternatives: one job per consultation (a 70-question consultation then fails as a unit); one job per batch (thousands of rows and a harder fan-in); SQS's visibility timeout as the lease (then the queue owns state the database should own). The failure to walk through: a worker runs out of memory at batch 3,000 of 5,000, its heartbeat goes stale after ten minutes, another worker takes over from the last checkpoint and only batch 3,000 is paid for twice. When the old process wakes, its fence is stale and it writes nothing. Weak point: 70 questions under a per-consultation cap of 4 means the reviewer sees questions arrive over hours, and the task list has to make that feel normal.

### 4. Human sign-off as a mandatory per-question state

With a preview on a sample, re-runnable after edits, and confirm-as-is in one click. No auto-approve in the MVP. Alternatives: auto-approve above a confidence threshold; one gate per consultation; tag first and review after. The published evaluation (`docs/01`, section 2) has reviewers correcting the model's mapping on roughly one response in four in the DWP report of 27 August 2025. That's the number I'd put in front of anyone asking why the gate exists. Trade-off: a second email and a wait. Weak point: whether one gate per question is right for a 70-question consultation is the thing I'm least sure of. Assignment, reminders and confirm-as-is soften it; user research on the sign-off screen decides it.

### 5. The data model

A long `answer` table with multi-select exploded; `respondent.attrs` as a write-once read model rebuilt from answers; immutable `theme_set_version` rows; tags never deleted; identity in a `vault` schema behind roles. Alternatives: a wide table per consultation (a schema per upload and no shared indexes); entity-attribute-value (slow and unreadable); a document store (loses the transactions everything else rests on). The filter query is `attrs @> '{"d_area": ["Villages"]}'` through the GIN index, joined to live `answer_theme` rows, and it's the query I'd write on a whiteboard. OFFSET is fine for page numbers; keyset is for export. Weak point: the plan is unbenchmarked on this schema. ADR 0006 in the Consult repo reports 218 ms for open responses filtered by three closed values on 500,000 respondents by 20 questions (read 18 September 2026), which says the shape works and nothing about this instance class. The acceptance test is an EXPLAIN showing a Bitmap Index Scan on the GIN index: at 20,000 rows in the proof-of-concept, at ten million in staging.

### 6. Model access through the gateway

Alias per consultation, recorded on every job; structured outputs plus code-side validation of everything that comes back; the synchronous lane by default with the provider's batch lane as a lever. Alternatives: provider SDKs called directly (keys and spend caps in our code); a self-hosted model (an operations burden the team doesn't need yet). A model change is configuration, gated by an evaluation on double-reviewed samples. Region: data at rest is in London; inference happens wherever the gateway's deployment for that alias runs, and the region is recorded per job so the DPIA states it instead of claiming UK-only. Weak point: wall-clock for a 100,000-response consultation is set by the gateway's throughput share, which is a negotiation, not code.

### 7. Generation and mapping shape

Generate from every answer, not a sample: a 5,000-answer sample sees a view held by one respondent in ten thousand only about two times in five (0.9999 to the power 5,000 is about 0.61). Batches of about 50, partitioned by the related closed answer; condense to about 30 with the longlist kept; map ten per prompt with enum labels and the two-way check; demographics never in a prompt. Alternatives: embed and cluster first (pgvector, a dependency and a tuning problem before the first theme); generate once on a sample (cheaper, misses the rare view); map one answer per call (ten times the calls for no accuracy I can point to). Weak point: partitioned generation can produce near-duplicate themes across partitions. Condensation has to merge them, and the preview's Other rate is the only early signal. A low Other rate isn't evidence of completeness; the human additions are. Measured in week one.

### 8. Email through an outbox row

Inserted in the transition transaction, relayed with `FOR UPDATE SKIP LOCKED` (worker fast path, reconciler slow path), Notify reference set to the outbox id, a link and nothing else in the body. Alternatives: send from the worker after commit (the dual write: a crash between the two loses or duplicates the email); SNS or EventBridge in the middle (another system to be at-least-once with). The same answer covers the database-to-SQS write at dispatch: commit, then send, and let the conditional claim absorb duplicates. The row goes `pending → sending → sent`: the relay marks it `sending` before the call, `sent` after, and the reconciler asks Notify by reference before resending anything left in `sending`, so a crash between the call and the `sent` write costs a lookup, not a duplicate. The key is `UNIQUE NULLS NOT DISTINCT (consultation_id, kind, theme_set_version_id, subject_id)`, so a milestone can only ever have one row. Weak point: a send Notify accepted but never acknowledged is the one window left, and it's stated rather than hidden. ADR-006 has the detail.

### 9. Validation, ingest and campaigns

The validator blocks errors and resolves warnings before spend; ingest is a job; exact duplicates are flagged at answer and respondent level, counted both ways, never deleted. Alternatives: validate in the request (impossible at 100,000 rows); reject unknown values (policy teams would fix spreadsheets by hand for a week); delete duplicates (a campaign is a legitimate response and the count matters). Weak point: near-duplicate clustering, for a proforma that respondents lightly edit, isn't in the MVP. It's the named next step, and I wouldn't describe it as shipped.

### 10. Fairness per department

A `department` table with a concurrency cap and a monthly budget in pence; round-robin dispatch when contended; pause with an attention email when over budget. Alternatives: first come first served (one 100,000-response consultation starves everyone for an afternoon); priority by size; a queue per department (an operational multiplication for nothing the caps don't give). Weak point: round-robin at dispatch is coarse, and the budget is an estimate in pence from our own token counts rather than the gateway's invoice. Reconciling the two is a monthly job for a person until it's automated.

### 11. Rollout in three increments, parity first

Section 8 has the table. Alternatives: build the full product and switch over; self-serve first. Weak point: the replay gate assumes the pilot consultations' archived outputs exist in a comparable form, and the sizing is a range with the usual caveat that the sign-off UX will take iterations.

### 12. Operated to three SLOs

Themes ready within four hours for a 100,000-response consultation at p95; the email within fifteen minutes of the state change; a dashboard filter under 500 ms at ten million answers. Alternative: no SLOs until there's a second department, which leaves nothing to argue with. Weak point: the four-hour figure assumes the 1M-TPM share in section 1. At 100,000 TPM it's nearer two days than one, which is why the batch lane exists as a lever. The 500 ms figure is a target, unproven on this schema until the staging load test.

## 8. Rollout, SLOs and what to measure

| Increment | Who uses it | What ships | Gate to the next |
|---|---|---|---|
| 1 (weeks 1 to 6, three engineers) | The analysts who run the pilot keep running it | The existing script and themefinder wrapped as the worker; upload, configure, validator, email, XLSX export | Replay the pilot consultations from their archived outputs and compare tags; the agreement rate is reported, whatever it is |
| 2 | The pilot departments, with i.AI analysts reviewing alongside | Sign-off with preview and re-run; the dashboard | Two consultations per department with stable reviewer edits |
| 3 | New departments, self-serve | Caps, budgets, the operator console, the report export | The manual script is switched off only after two consultations per department match |

Sizing: three engineers plus a designer and a user researcher part-time, two quarters to increment 3. Week-one measurements: theme quality under partitioned generation; reviewer time per question; edits per hundred tags; cost per consultation; the share of consultations that complete with no operator touch.

Cost is a guard (the estimate at Confirm, the department budget, the gateway cap), not something to optimise. The DSIT and Defra press release of 16 October 2025 reported 50,000-plus responses themed for £240 alongside 22 hours of expert checking (URL and retrieval date in `docs/01`). Reviewer time is what binds; `docs/05` has the arithmetic.

## 9. Failure handling

The five for the runbook first.

| Failure | What happens | What stops it doing damage |
|---|---|---|
| Worker dies mid-job | Heartbeat goes stale; the reconciler re-sends; another worker claims and resumes from the last checkpoint | The fence: the old process can't write after takeover |
| Queue message lost or duplicated | The job table is authoritative; reconciler statement 2 re-sends a stale `queued` job | The conditional claim: a duplicate message for a live lease gets zero rows |
| Gateway 429 or 5xx | Full-jitter backoff, 1 to 60 s, six attempts, under the semaphore | A spend-cap 429 pages a human instead of retrying |
| Bad model output | The two-way check and the enum reject the batch; retry at size 1; the answer goes to `unprocessable` | The nine neighbours in the batch are unaffected |
| Email | Outbox row in the commit; SKIP LOCKED relay; Notify reference | Effectively once, with the stated window |

Then the rest, briefly. Two reviewers on one question: the version guard, and the loser sees a conflict message. Definition doesn't match the responses: the validator, before spend. Over-condensation: the longlist, the preview, and human additions as the real coverage signal. Campaigns: flagged at both levels, toggled on the dashboard, never deleted. A stuck consultation: every running state has a failed edge, an operator retry and an alarm, and the reconciler re-runs the fan-ins. Deploys: `stopTimeout` 120 s with batches sized to finish inside it, and expand-and-contract migrations. A model retired mid-programme: the alias is pinned per consultation, a reopened consultation runs on the new alias as a new theme-set version, and the manifest records both.

## 10. Security and governance

The threat model and the full notes belong to PR-02 (`THREAT_MODEL.md`, `docs/06`). The commitments the architecture makes:

- OFFICIAL at rest in London: SSE-KMS on S3, encrypted RDS.
- Only the question text, the open answer and, where linked, the related closed answer reach the model. Demographics and identifiers never do. The inference region is recorded per job.
- Responses are untrusted input. No tools, answers as JSON data after a preamble, labels constrained to an enum, small batches. It's designed for containment. I wouldn't claim resistance.
- Departments are controllers and the platform is a processor. `department_id` is on every table with a mandatory query scope; row-level security is the escape hatch if scoping in code proves leaky.
- The manifest export pre-fills DPIA and Algorithmic Transparency Recording Standard fields. A production service needs its own ATRS record.
- `retention_until` drives the S3 lifecycle and a deletion job. Erasure of one respondent removes their answers, tags, theme examples and vault row, and regenerates exports. Exports already sent are the department's to recall.
- Welsh-language responses have to be treated no less favourably than English ones for consultations covering Wales, under the department's Welsh language scheme or the Welsh Language Standards as applicable (`docs/06` carries the reference). A human-read lane for them is a dated gap, not an exclusion.
- A staging environment and a load test at two concurrent 100,000-response consultations before any cross-government go-live.
- The Service Standard applies to a cross-government service: a service owner, user research on the sign-off screen, an assessment.

## 11. What I'd cut, in order, and what I wouldn't

Cut, in this order if asked: the provider batch lane; near-duplicate clustering; the stance pass; the cross-tab UI (ship it in the XLSX instead); embeddings and pgvector; a theme hierarchy; worker autoscaling (one fixed worker); Notify delivery callbacks; per-consultation KMS keys, row-level security and PII NER (keep department scoping, the vault split and regex masking); the long-form and Welsh lanes, named and dated.

Never cut: the validator with resolutions; the ingest job; per-question find-themes with lease, fence and checkpoints; sign-off with preview and re-run; map-themes with the two-way check; both fan-ins with Notify; per-response tag editing (it's where the agreement number comes from); closed-question charts; the filterable dashboard with the XLSX and report exports. If the two lists ever conflict, the keep list wins.

## 12. The five screens

ASCII wireframes with the calls each screen makes. The forms screens are server-rendered and post to these same endpoints; the dashboard island calls them as JSON. Question and option names below are fictional (a made-up consultation on a riverside cycle route) and share nothing with any real consultation.

### Screen 1. Create, then configure

```
 Create a consultation
 +--------------------------------------------------------------------+
 | Name          [ Riverside cycle route: proposed changes          ] |
 | Source        ( ) Citizen Space  ( ) Qualtrics  (x) Generic        |
 | Responses     [ Choose file ] responses.xlsx  (uploading 62%)      |
 | Definition    [ Choose file ] definition.xlsx  (optional)          |
 |                                                  [ Continue ]      |
 +--------------------------------------------------------------------+

 Configure the questions               Validation: 2 warnings, 0 errors
 +--------------------------------------------------------------------+
 | Column           Kind                        Detail                |
 | respondent_ref   Respondent id                                     |
 | email            Identity (vault)                                  |
 | d_area           Demographic     4 values seen: Town centre (812), |
 |                                  Suburbs (640), Villages (301) ... |
 | d_commute        Demographic     N/A seen 1,204 times: keep as a   |
 |                                  value (x)  treat as not answered  |
 | c_route          Closed, single  Support / Oppose / Not sure       |
 |   ! 14 rows say "Unsure"  -> [ Map to "Not sure" v ]               |
 | o_reason         Open, follow-up to c_route                        |
 | o_safety         Open                                              |
 | notes_internal   Ignore                                            |
 +--------------------------------------------------------------------+
 | Check your answers                                                 |
 |   1,987 respondents, 2 open questions, ~4,100 open answers         |
 |   Estimated model use: ~2.3M tokens, about £3.30                   |
 |                                                  [ Confirm ]       |
 +--------------------------------------------------------------------+
```

Calls: `POST /consultations` (name, source) returns the id. `POST /consultations/{id}/upload-url` returns a presigned multipart URL and the browser uploads to S3. `POST /consultations/{id}/headers` confirms the header row: 202, `stage` job. `GET /consultations/{id}/validation` returns the report. `PUT /consultations/{id}/configuration` takes column kinds, options, follow-up links, value policies and warning resolutions. `POST /consultations/{id}/confirm`: 202, `ingest` job.

### Screen 2. Status and the task list

```
 Riverside cycle route: proposed changes         Status: Awaiting review
 +--------------------------------------------------------------------+
 | ! One question needs attention: o_safety failed to find themes.    |
 |   An operator has been notified.                                   |
 +--------------------------------------------------------------------+
 | Question   State             Assigned to    Since        Action    |
 | o_reason   Themes ready      A. Reviewer    2 days       [Review]  |
 | o_safety   Attention needed  -              -            -         |
 +--------------------------------------------------------------------+
 | You've had the email for themes ready. You'll get one more when    |
 | the analysis is complete. o_safety's retry won't send an email.    |
 +--------------------------------------------------------------------+
```

Calls: `GET /consultations/{id}` returns status, `attention_reason` and counts. `GET /consultations/{id}/questions` returns state, `assigned_to` and `review_started_at` per open question. `POST /questions/{id}/assign` takes a user. The page polls `GET /consultations/{id}` while a job is running, and nothing depends on the poll, because the email is the completion signal.

### Screen 3. Theme sign-off

```
 o_reason: Why do you feel that way about the route?   Draft v1, edited
 +--------------------------------------------------------------------+
 | Preview on 200 answers: Other 6%   Counts from the AI's original   |
 |                                    list. [ Re-run preview ]        |
 +--------------------------------------------------------------------+
 | #  Theme                          Preview  Quotes                  |
 | 1  Junction safety at the bridge     58    "the bridge junction..."|
 | 2  Lighting after dark               41    "unlit for half a..."   |
 | 3  Loss of parking on Mill Lane      27    "we'd lose the..."      |
 | 4  Flooding on the towpath           19    "underwater every..."   |
 | 5  Cost to the council               11    "money better spent..." |
 |    [ Rename ] [ Merge with... ] [ Split ] [ Remove ]               |
 |                                                      [ + Add ]     |
 +--------------------------------------------------------------------+
 | Longlist (12 candidates folded into the above)         [ Show ]    |
 +--------------------------------------------------------------------+
 | [ Confirm as-is ]    [ Confirm edited list ]                       |
 +--------------------------------------------------------------------+
```

Calls: `GET /questions/{id}/theme-set` returns the current candidate version with themes, longlist, preview counts, quotes and `version`. `PATCH /theme-sets/{id}` takes `expected_version` and one edit (rename, merge, split, add, remove) and returns 409 on a stale version. `POST /questions/{id}/preview`: 202, `preview_themes` job. `POST /questions/{id}/sign-off` with `expected_version`: 200 and a `map_themes` job, or 409 if another reviewer got there first.

### Screen 4. Per-question dashboard

```
 o_reason: Why do you feel that way about the route?
 +--------------------------------------------------------------------+
 | Filters  [ d_area = Villages x ] [ c_route = Oppose x ] [ + Add ]  |
 |          Showing 212 of 1,987 respondents who answered             |
 +--------------------------------------------------------------------+
 | Theme                          Respondents   % of 212              |
 | Loss of parking on Mill Lane        96       45%  ############     |
 | Junction safety at the bridge       61       29%  ########         |
 | Flooding on the towpath             40       19%  #####            |
 | Other                               14        7%  ##               |
 +--------------------------------------------------------------------+
 | c_route among these respondents: Oppose 212 (filtered)             |
 +--------------------------------------------------------------------+
 | Responses                                          Page 3 of 11    |
 | +----------------------------------------------------------------+ |
 | | "We'd lose the only parking near the surgery ..."              | |
 | | [AI] Loss of parking on Mill Lane   [Human] Junction safety  x | |
 | |                                              [ + Add tag ]     | |
 | +----------------------------------------------------------------+ |
 | | "The towpath is underwater every winter, so ..."               | |
 | | [AI] Flooding on the towpath                                   | |
 | +----------------------------------------------------------------+ |
 |                                  < Prev  1 2 [3] 4 ... 11  Next >  |
 +--------------------------------------------------------------------+
```

Calls, each reading the filter from the query string `?f=attr:d_area=Villages&f=attr:c_route=Oppose`: `GET /questions/{id}/theme-counts?f=...` returns rows with the denominator. `GET /questions/{id}/related-distribution?f=...`. `GET /questions/{id}/responses?f=...&page=3` returns cards with live tags and their `source`. `POST /answers/{id}/tags` (theme key) and `POST /answers/{id}/tags/{theme}/retract`, each writing an `audit_event`. The `other:` predicate, for "what did people tagged Lighting on o_safety say here", is `f=other:o_safety.theme=lighting_after_dark` and compiles to a semi-join.

### Screen 5. Overview and export

```
 Riverside cycle route: proposed changes                 Status: Ready
 +--------------------------------------------------------------------+
 | 1,987 respondents   2 open questions   Time in review: 3.5 days    |
 +--------------------------------------------------------------------+
 | Which part of the district?      Support the proposed route?       |
 | Town centre   812  ##########    Support    1,021  ############    |
 | Suburbs       640  ########      Oppose       704  ########        |
 | Villages      301  ####          Not sure     262  ###             |
 | Outside       234  ###                                             |
 +--------------------------------------------------------------------+
 | Open questions                                                     |
 | o_reason   5 themes   1,912 tagged   34 unprocessable   [ Explore ]|
 | o_safety   4 themes   1,860 tagged    0 unprocessable   [ Explore ]|
 +--------------------------------------------------------------------+
 | Export   [ Spreadsheet with themes ]   [ Report ]                  |
 |          Last export: 18 Sep, by A. Reviewer  (download)           |
 +--------------------------------------------------------------------+
```

Calls: `GET /consultations/{id}/overview` returns counts, a distribution per demographic and closed question, the review-time KPI and a per-question summary. `POST /consultations/{id}/exports` (kind `xlsx` or `report`): 202, an `export` or `report` job. `GET /exports/{id}` returns status and, when done, a short-lived presigned S3 link.

## 13. Where to go next

`docs/03-adrs/` for the decisions that needed a full record; `docs/04` for the sixteen tables and every index; `docs/05` for the cost arithmetic; `docs/06` and `THREAT_MODEL.md` for the rest of section 10; `poc/` (from PR-03) for the four mechanics to be proved by a test rather than asserted: the fan-in transaction, lease takeover with a fence, idempotent tag inserts and the indexed filter query.

## Correction, 19 September 2026

A review of PR-02 found fifteen inconsistencies across the design documents; `docs/07-reviews.md` logs the pass and PR-02b reconciles them. The entries below correct this file. Each names the section it corrects and gives the corrected text; the body above is left as it merged. `docs/04` is the one document rewritten in place, because the schema is typed from it.

1. **Section 6, the reopen row and the paragraph after the table.** The row read that the later `ready` email "carries that version as `subject_id`". It carries the pass, not the version:

   | From | To | Trigger | Written by | Email |
   |---|---|---|---|---|
   | `ready` | `awaiting_review` | A question reopened for correction, or re-run on a new model alias; a new candidate theme-set version, and a new `run_id` minted in the same transaction so the later `ready` email has a row of its own | Web app, on a reviewer's action, through `advance_consultation` | No |

   Add to the paragraph on `advance_consultation`: `run_id` is minted with the consultation row, copied onto every job a pass inserts, and replaced by the reopen. Every milestone outbox row carries the current one as `subject_id`. That is what keeps a reopened question's second `map_themes` job, and the second `analysis_ready` email, from colliding with the first (`docs/04`, sections 2 and 3).

2. **Steps 7 and 10, the outbox insert.** Both inserts read the pass id from the locked row rather than writing bare values:

   ```sql
   -- only if the UPDATE above touched one row:
   INSERT INTO notification_outbox (department_id, consultation_id, kind, subject_id)
   SELECT department_id, id, 'themes_ready', run_id
     FROM consultation WHERE id = $c
   ON CONFLICT DO NOTHING;
   ```

   Step 10 is the same with `'analysis_ready'`.

3. **Section 7, decision 8, the key.** The key is `UNIQUE NULLS NOT DISTINCT (consultation_id, kind, subject_id)`. `theme_set_version_id` is gone from the outbox: once milestone rows took the pass id, nothing wrote it. A milestone row carries the pass's `run_id`; an attention row the failed job's id, or null when a paused budget is the reason and there is no job, which is what the modifier is for; a reminder the question's id. ADR-006 carries the same correction.

4. **Step 2, the staging table.** "COPYs the file into a staging table" means one logged table per upload in a `staging` schema the pipeline role can't read. The identity columns sit in it until ingest moves them to the vault, and it has to outlive the human configure step, which an unlogged table can't: Postgres truncates those on crash recovery (`docs/04`, section 2, with the log row in `docs/01`).

5. **Step 3a, ingest.** Ingest reads the staging table as the ingest role and drops it once its transaction has committed. If the table is missing at Confirm (a restore, a hand drop), the ingest job re-runs the stage step from the S3 original, same sha256, before it ingests. A second `stage` job row would collide on `job_one_per_run` under the same `run_id`, which is why the ingest job does the re-run itself rather than Confirm inserting another job.
