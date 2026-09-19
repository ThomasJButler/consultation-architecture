# Data model

The tables, keys and indexes behind `docs/02-architecture.md`, written so the proof-of-concept's `schema.sql` (PR-03) can be typed from it. Section 4 of docs/02 is the pipeline these tables serve, section 5 the reconciler, section 6 the consultation transition table. Where a mechanic depends on a Postgres behaviour, the fact is cited to the verification log in `docs/01-research.md`, section 6. Nothing here runs yet. The DDL is a sketch I've checked by eye, and the queries are the ones PR-09 will put under `EXPLAIN`.

Two conventions, stated once. Every table carries `department_id uuid NOT NULL`, because departments are controllers and every query goes through a mandatory scope (docs/02, section 10); it appears in the DDL and is left out of the key-column lists below. Statuses are `text` with a `CHECK` rather than enum types: adding a state is then a constraint change that expand-and-contract migrations (docs/02, section 9) handle, and an enum value can never be dropped.

## 1. The sixteen tables

| Table | Key columns | Purpose |
|---|---|---|
| `department` | `id`, `name`, `concurrent_jobs_cap`, `monthly_budget_pence`, `contact_email`, `paused_at`, `pause_id` | The tenant. The dispatcher reads the cap and the budget (docs/02, step 4). A pause, by budget or by an operator, is one guarded UPDATE (`WHERE paused_at IS NULL`) that sets `paused_at` and mints `pause_id`, the subject of the attention rows it writes (section 2); a resume clears both. |
| `consultation` | `id`, `department_id`, `name`, `source`, `status` draft / staging / staged / processing / awaiting_review / ready, `attention_reason`, `run_id`, `status_changed_at`, `awaiting_review_at`, `model_alias`, `retention_until`, `created_by`, `upload_sha256`, `row_count`, `column_roles jsonb` | One upload. `status` is derived from the questions (docs/02, section 6); `attention_reason` is nullable and orthogonal to it. `run_id` names the current pass: minted with the row, copied onto every job the pass inserts, replaced by a reopen (section 2). `advance_consultation` stamps `status_changed_at` on every transition and `awaiting_review_at` on entry to that state: the first feeds the stuck-consultation alarm, the pair the review-time KPI on the overview (docs/02, step 8 and screen 5), latest pass only. `column_roles` records the columns that aren't questions: respondent id and ignore. |
| `question` | `id`, `consultation_id`, `column_ref`, `question_text`, `kind` demographic / closed / open / identity, `response_type`, `ordinal`, `related_closed_question_id`, `value_policy jsonb`, `status`, `assigned_to`, `review_started_at` | One column of the responses file, as configured in the app (docs/02, step 3). `status` is the per-question state machine and stays null unless `kind` is open. `value_policy` holds the N/A decision and any unknown-value resolutions. An identity column gets a row so the export can name it, and no answer rows. |
| `question_option` | `id`, `question_id`, `label`, `ordinal` | The option vocabulary of a closed question. Options are rows, so a label can contain a comma (docs/00). |
| `respondent` | `id`, `consultation_id`, `external_id`, `source_row_no`, `attrs jsonb`, `duplicate_of` | One row of the file. `attrs` is the filter read model (section 5). There's no `raw_row`: it would carry the identity columns past the vault, and the original file in S3 keyed by its sha256 plus `source_row_no` is the audit copy. |
| `vault.respondent_identity` | `respondent_id`, `column_ref`, `value_text` | Name, email, postcode, whatever was marked identity. Its own schema so the grants can differ: the ingest role inserts, the export role selects, the pipeline role has no grant, and only `consult_admin` deletes, for the deletion job and the console's erasure action; the worker never connects as it (docs/06, section 2.4). |
| `answer` | `id bigint`, `consultation_id`, `respondent_id`, `question_id`, `option_id`, `value_text`, `is_blank`, `text_sha256`, `duplicate_of_answer_id`, `tsv` | The long table. One row per respondent per question; a multi-select answer is one row per chosen option (docs/02, step 3a). A not-answered cell is a row with `is_blank` set, so the denominator can be counted. |
| `theme_set_version` | `id`, `question_id`, `version_no`, `status` candidate / signed_off / superseded, `parent_version_id`, `edit_version`, `signed_off_by`, `signed_off_at` | An immutable list. v1 is the candidate, v2 the signed-off copy, v3 a reopen (ADR-004). `edit_version` is the counter the sign-off screen's `expected_version` checks (docs/02, screen 3); `version_no` is lineage. |
| `theme` | `id`, `theme_set_version_id`, `key`, `label`, `description`, `is_longlist`, `is_fallback`, `lineage_theme_id`, `preview_count` | One theme in one version. `key` is the enum value the model returns; `is_fallback` marks `OTHER` and `NO_REASON`; `lineage_theme_id` points at the candidate it was condensed from. |
| `theme_example` | `theme_id`, `answer_id`, `rank` | The quotes on the sign-off screen, from the 200-answer preview. |
| `answer_theme` | `id`, `answer_id`, `theme_id`, `theme_set_version_id`, `job_id`, `batch_no`, `source` ai / human, `user_id`, `created_at`, `retracted_at`, `retracted_by` | A tag. Never deleted; retracted in place by setting `retracted_at`, re-added by clearing it (docs/02, step 11). |
| `job` | `id`, `consultation_id`, `question_id`, `kind` stage / ingest / find_themes / preview_themes / map_themes / export / report / erasure, `run_id`, `created_at`, `status`, `attempts`, `next_attempt_at`, `claimed_by`, `heartbeat_at`, `sent_at`, `model_alias`, `prompt_sha256`, `params`, `tokens_in`, `tokens_cached`, `tokens_out`, `cost_pence`, `error_code`, `provider_request_id` | The unit of work and the ledger. `claimed_by`, `attempts` and `heartbeat_at` are the lease and the fence (docs/02, step 5); `created_at` is what the job-age alarm reads (docs/02, section 3). `error_code` and `provider_request_id` are all a failure stores; they are the two columns behind what docs/02 section 3.4 calls `job.error`, and there is no column a message body could go in. |
| `job_batch` | `job_id`, `batch_no`, `stage`, `answer_ids bigint[]`, `status`, `trace_id`, `tokens_in`, `tokens_out`, `finished_at` | One checkpoint: one call of one stage function on one chunk (docs/02, step 6). A batch that failed the two-way check at size 1 is a row with status `unprocessable`, which is where the dashboard's bucket comes from. `trace_id` is the gateway's trace id for the call, so an erasure can find and delete every trace that holds an answer; one answer sits in several batches (generation, preview, mapping) and a duplicate in none (docs/06, section 4). |
| `notification_outbox` | `id`, `consultation_id`, `kind`, `subject_id`, `status` pending / sending / sent, `notify_id`, `created_at`, `sent_at` | The email, written in the transition's commit (ADR-006). Every row names its subject. A milestone row carries the consultation's `run_id`, so the first pass's `analysis_ready` and the one a reopen earns are two rows and the second isn't dropped on the first (section 2 has the INSERT). An attention row carries the failed job's id, or the department's `pause_id` when a budget pause is the reason, so a department paused every month gets a row per pause. A reminder carries the id of the candidate `theme_set_version` awaiting sign-off, which a reopen replaces, so a reopened question can be reminded again. The one collision left is a job that fails again after an operator retry: it lands on its first row and sends nothing, and the operator who retried it is at the console, where the failure shows. |
| `audit_event` | `id`, `consultation_id`, `actor_id`, `action`, `subject_table`, `subject_id`, `before jsonb`, `after jsonb`, `at` | Append-only: sign-off, tag edits, operator actions. The app role gets INSERT and SELECT and nothing else. |
| `export` | `id`, `consultation_id`, `kind` xlsx / report, `job_id`, `s3_key`, `created_by`, `created_at`, `superseded_at` | What `GET /exports/{id}` reads and the overview's last-export line shows (docs/02, screen 5). Erasure regenerates exports, which needs a list of them. |

## 2. DDL sketch for the eight tables the mechanics rest on

A sketch in Postgres 17 syntax, checked by eye, not yet run. `department`, `question_option`, `theme_set_version` and `theme` are referenced as though created first; their mechanical content is one unique key each, listed in section 3. Foreign keys to `department` are left implicit after the first table. The `stage` job's staging table isn't one of the sixteen. It's one logged table per upload in a `staging` schema (`staging."<consultation id>"`), COPYed into by the `stage` job and read by the `ingest` job, both running as the ingest role; the pipeline role has no grant on the schema, because the identity columns sit there until ingest moves them to the vault. It has to be logged: it lives across the human configure step, and an unlogged table "is automatically truncated after a crash or unclean shutdown" (docs/01, section 6; log row "Postgres UNLOGGED tables", checked 19 September 2026). The ingest job drops it after its transaction commits. If it's missing at Confirm, the ingest job re-runs the stage step from the S3 original first (docs/02, step 3a).

```sql
CREATE TABLE consultation (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  department_id    uuid NOT NULL REFERENCES department (id),
  name             text NOT NULL,
  source           text NOT NULL CHECK (source IN ('citizen_space', 'qualtrics', 'generic')),
  status           text NOT NULL DEFAULT 'draft' CHECK (status IN
                     ('draft', 'staging', 'staged', 'processing', 'awaiting_review', 'ready')),
  attention_reason text,
  run_id           uuid NOT NULL DEFAULT gen_random_uuid(),
  status_changed_at timestamptz NOT NULL DEFAULT now(),
  awaiting_review_at timestamptz,
  model_alias      text,
  retention_until  date,          -- five years at the most: the S3 lifecycle backstop
                                  -- in docs/06 expires objects at five years whatever
                                  -- this says, so a longer date would be a lie
  created_by       uuid NOT NULL,
  upload_sha256    bytea,
  row_count        integer,
  column_roles     jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE question (
  id                         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  department_id              uuid NOT NULL,
  consultation_id            uuid NOT NULL REFERENCES consultation (id),
  column_ref                 text NOT NULL,
  question_text              text NOT NULL,
  kind                       text NOT NULL CHECK (kind IN ('demographic', 'closed', 'open', 'identity')),
  response_type              text CHECK (response_type IN ('single_select', 'likert_5', 'multi_select')),
  ordinal                    integer NOT NULL,
  related_closed_question_id uuid REFERENCES question (id),
  value_policy               jsonb NOT NULL DEFAULT '{}',
  status                     text CHECK (status IN ('configured', 'finding_themes', 'themes_ready',
                               'signed_off', 'assigning_themes', 'complete', 'find_failed', 'map_failed')),
  assigned_to                uuid,
  review_started_at          timestamptz,
  UNIQUE (consultation_id, column_ref)
);

CREATE TABLE respondent (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  department_id   uuid NOT NULL,
  consultation_id uuid NOT NULL REFERENCES consultation (id),
  external_id     text,
  source_row_no   integer NOT NULL,
  attrs           jsonb NOT NULL DEFAULT '{}',
  duplicate_of    bigint REFERENCES respondent (id),
  UNIQUE (consultation_id, source_row_no)
);
CREATE INDEX respondent_attrs_gin ON respondent USING gin (attrs jsonb_path_ops);
CREATE UNIQUE INDEX respondent_external_id ON respondent (consultation_id, external_id)
  WHERE external_id IS NOT NULL;

CREATE TABLE answer (
  id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  department_id          uuid NOT NULL,
  consultation_id        uuid NOT NULL REFERENCES consultation (id),
  respondent_id          bigint NOT NULL REFERENCES respondent (id),
  question_id            uuid NOT NULL REFERENCES question (id),
  option_id              uuid REFERENCES question_option (id),
  value_text             text,
  is_blank               boolean NOT NULL DEFAULT false,
  text_sha256            bytea,
  duplicate_of_answer_id bigint REFERENCES answer (id),
  tsv                    tsvector GENERATED ALWAYS AS
                           (to_tsvector('english', coalesce(value_text, ''))) STORED,
  UNIQUE NULLS NOT DISTINCT (respondent_id, question_id, option_id)
);
CREATE INDEX answer_question_id_id ON answer (question_id, id);
CREATE INDEX answer_question_sha   ON answer (question_id, text_sha256);
CREATE INDEX answer_tsv_gin        ON answer USING gin (tsv);

CREATE TABLE job (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  department_id       uuid NOT NULL,
  consultation_id     uuid NOT NULL REFERENCES consultation (id),
  question_id         uuid REFERENCES question (id),
  kind                text NOT NULL CHECK (kind IN ('stage', 'ingest', 'find_themes',
                        'preview_themes', 'map_themes', 'export', 'report', 'erasure')),
  run_id              uuid NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  status              text NOT NULL DEFAULT 'pending' CHECK (status IN
                        ('pending', 'queued', 'running', 'succeeded', 'failed_retryable', 'failed')),
  attempts            integer NOT NULL DEFAULT 0,
  next_attempt_at     timestamptz,
  claimed_by          text,
  heartbeat_at        timestamptz,
  sent_at             timestamptz,
  model_alias         text,
  prompt_sha256       bytea,
  params              jsonb NOT NULL DEFAULT '{}',
  tokens_in bigint NOT NULL DEFAULT 0, tokens_cached bigint NOT NULL DEFAULT 0,
  tokens_out bigint NOT NULL DEFAULT 0, cost_pence integer NOT NULL DEFAULT 0,
  error_code text, provider_request_id text
);
CREATE UNIQUE INDEX job_one_per_run ON job (consultation_id, question_id, kind, run_id)
  NULLS NOT DISTINCT WHERE kind IN ('stage', 'ingest', 'find_themes', 'map_themes');
CREATE INDEX job_by_status ON job (status);

CREATE TABLE job_batch (
  job_id      uuid NOT NULL REFERENCES job (id),
  batch_no    integer NOT NULL,
  stage       text NOT NULL,
  answer_ids  bigint[] NOT NULL,
  status      text NOT NULL CHECK (status IN ('done', 'unprocessable')),
  trace_id    text,
  tokens_in integer NOT NULL DEFAULT 0, tokens_out integer NOT NULL DEFAULT 0,
  finished_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, batch_no)
);
CREATE INDEX job_batch_answer_ids_gin ON job_batch USING gin (answer_ids);

CREATE TABLE answer_theme (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  department_id        uuid NOT NULL,
  answer_id            bigint NOT NULL REFERENCES answer (id),
  theme_id             uuid NOT NULL REFERENCES theme (id),
  theme_set_version_id uuid NOT NULL REFERENCES theme_set_version (id),
  job_id               uuid REFERENCES job (id),
  batch_no             integer,
  source               text NOT NULL CHECK (source IN ('ai', 'human')),
  user_id              uuid,
  created_at           timestamptz NOT NULL DEFAULT now(),
  retracted_at         timestamptz,
  retracted_by         uuid,
  UNIQUE (answer_id, theme_id, theme_set_version_id)
);
CREATE INDEX answer_theme_live ON answer_theme (theme_set_version_id, theme_id, answer_id)
  WHERE retracted_at IS NULL;

CREATE TABLE notification_outbox (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  department_id        uuid NOT NULL,
  consultation_id      uuid NOT NULL REFERENCES consultation (id),
  kind                 text NOT NULL CHECK (kind IN
                         ('themes_ready', 'analysis_ready', 'attention_needed', 'review_reminder')),
  subject_id           uuid NOT NULL,
  status               text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sending', 'sent')),
  notify_id            text,
  created_at           timestamptz NOT NULL DEFAULT now(),
  sent_at              timestamptz,
  UNIQUE (consultation_id, kind, subject_id)
);
CREATE INDEX notification_outbox_pending ON notification_outbox (id) WHERE status = 'pending';
```

Three statements the outbox key rests on. The first is the milestone insert inside `advance_consultation` (docs/02, section 6), run only when the guarded `UPDATE consultation` touched a row. The other two are the reopen's two edges, same routine, same row lock. A reopen for correction puts the question back at `themes_ready` with a new candidate `theme_set_version` to edit, and the consultation at `awaiting_review`. A re-run on a new model alias puts the question back at `finding_themes` with a new `find_themes` job, and the consultation at `processing`, so fan-in 1 flips it and sends the email exactly as on the first pass. Both mint a new `run_id`.

```sql
INSERT INTO notification_outbox (department_id, consultation_id, kind, subject_id)
SELECT department_id, id, 'analysis_ready', run_id
  FROM consultation WHERE id = $c
ON CONFLICT DO NOTHING;

UPDATE consultation SET status = 'awaiting_review', run_id = gen_random_uuid(),
       status_changed_at = now(), awaiting_review_at = now()
 WHERE id = $c AND status = 'ready';

UPDATE consultation SET status = 'processing', run_id = gen_random_uuid(), model_alias = $alias,
       status_changed_at = now(), awaiting_review_at = NULL
 WHERE id = $c AND status = 'ready';
```

The `SELECT` runs under the lock the routine took first, so it reads whatever `run_id` the transaction holds, including one a reopen has just minted. Every job the reopen inserts copies the new `run_id`, which is why the reopened question's second `map_themes` job sits beside its first under `job_one_per_run` instead of colliding with it. A budget pause is the same shape on `department`: `UPDATE department SET paused_at = now(), pause_id = gen_random_uuid() WHERE id = $d AND paused_at IS NULL RETURNING pause_id`, then one attention row per consultation of the department with a pending job, carrying that `pause_id`. The guard fires once per pause, so the reconciler's repeated dispatch runs can't multiply the rows, and next month's pause mints a new id.

`answer_theme.batch_no` where docs/02 step 9 and ADR-004 say `batch_id`, because `job_batch`'s key is `(job_id, batch_no)` and the tag already carries `job_id`; the pair `(job_id, batch_no)` is the batch id. The unique key on `job` is partial for a different reason: previews, exports and erasures repeat by design, so the one-job-per-run rule covers the four pipeline kinds only.

## 3. Every unique index, and what it makes idempotent

| Index | What it makes idempotent |
|---|---|
| `question (consultation_id, column_ref)` | Saving the configure screen twice, or the importer running after a hand edit, upserts a column rather than duplicating it. The leading column is what the fan-in's `NOT EXISTS` scans. |
| `question_option (question_id, label)` | The "add it as an option" resolution applied twice yields one option. |
| `respondent (consultation_id, source_row_no)` | A second delivery of the `ingest` message finds the rows already there and the job finishes without a second copy. |
| `respondent (consultation_id, external_id)`, partial on non-null | Two file rows with one respondent id are one person twice or a broken export, and neither should land silently: the validator reports it before spend (docs/02, section 3.2) and ingest refuses it rather than guessing. |
| `answer NULLS NOT DISTINCT (respondent_id, question_id, option_id)` | The same for exploded answer rows. The modifier is what makes a free-text answer with a null `option_id` collide with itself (docs/01, section 4). Its prefix also serves the other-question semi-join. |
| `theme_set_version (question_id, version_no)` | A `find_themes` job delivered twice, where the first delivery had already finished, can't write a second v1; two reviewers confirming can't freeze two v2s. |
| `theme (theme_set_version_id, key)` | The enum the model returns has one meaning per key, and a re-run of condensation writes each key once. |
| `theme_example (theme_id, answer_id)` | A re-run preview keeps one quote per answer per theme. |
| `answer_theme (answer_id, theme_id, theme_set_version_id)`, full, not partial | A `map_themes` batch replayed after takeover inserts nothing new, and a resumed run can't re-insert a tag a person has retracted: the conflict lands on the retracted row and does nothing (ADR-004). PR-05 will pin both. |
| `job (consultation_id, question_id, kind, run_id) NULLS NOT DISTINCT`, partial on the four pipeline kinds | One job per (consultation, open question, kind, run) is enforced by the index, so a code path that forgets the rule gets a conflict and not a second job (docs/02, section 7, decision 3). `question_id` is null for `stage` and `ingest`, hence the modifier. `run_id` is minted with the consultation row and replaced by a reopen (section 2), so a reopened question's second `map_themes` job carries a different `run_id` and gets a slot of its own. |
| `job_batch (job_id, batch_no)` | The checkpoint. A worker taking over reads the last finished batch and starts at the next; a half-written batch's `ON CONFLICT DO NOTHING` lands here (ADR-002). |
| `notification_outbox (consultation_id, kind, subject_id)` | One email per milestone per pass. The worker's fan-in, the reconciler's fourth statement re-running the same predicate, and both relays all meet on one row. `subject_id` is `NOT NULL`, so the key needs no `NULLS NOT DISTINCT`: every kind of row names what it's about (section 1). |
| `vault.respondent_identity (respondent_id, column_ref)` | Ingest replayed writes one identity value per column. |

## 4. Every other index, and the query it serves

| Index | Query |
|---|---|
| GIN `jsonb_path_ops` on `respondent.attrs` | Every `attr:` predicate, as `attrs @> $doc`. The class supports containment only and is usually much smaller than the default (docs/01, section 4). |
| `answer (question_id, id)` | The per-question scan under every dashboard query, the `ORDER BY a.id` behind OFFSET pages, and the keyset cursor for export. |
| `answer (question_id, text_sha256)` | `duplicate_of_answer_id` at ingest, and the campaign toggle's count of identical answers. |
| GIN on `answer.tsv` | The search box on the per-question view, `tsv @@ plainto_tsquery('english', $term)` ANDed with the question predicate. English only for now; a Welsh configuration belongs to the lane docs/02 section 10 dates. |
| `answer_theme (theme_set_version_id, theme_id, answer_id) WHERE retracted_at IS NULL` | Theme counts under a filter. Live rows only, so a version with many retractions stays cheap to count; the full unique index handles the per-answer lookup. |
| `job (status)` | The reconciler's five scans. The table is small enough that the planner may ignore it, which is fine. |
| GIN on `job_batch.answer_ids` | Erasure's trace lookup, `answer_ids @> ARRAY[$id]::bigint[]`: every batch that carried an answer, so every trace to delete (docs/06, section 4). |
| `notification_outbox (id) WHERE status = 'pending'` | The relay's `ORDER BY id FOR UPDATE SKIP LOCKED` (ADR-006). |
| `audit_event (consultation_id, at)` | The history behind a tag or a sign-off, newest first. |

## 5. `respondent.attrs`

Built once by the ingest job from the answer rows and never edited by hand. Every demographic and every closed answer, keyed by column reference; values are always arrays, so a single-select and a multi-select answer take the same test. Open answers aren't in it and the prompt path never reads it. In the fictional consultation docs/02's wireframes use, one respondent's document might be:

```json
{"d_area": ["Villages"], "d_commute": ["N/A"], "c_route": ["Oppose"], "c_modes": ["Cycle", "Walk"]}
```

`d_commute` is there as `N/A` because that question's value policy keeps it as a value (docs/02, section 3.2). A column the respondent skipped is absent, which is what makes the last row of this table work.

| Filter in the URL | Predicate |
|---|---|
| `attr:d_area=Villages` | `attrs @> '{"d_area": ["Villages"]}'` |
| Two values in one group, OR | `attrs @> '{"d_area": ["Villages"]}' OR attrs @> '{"d_area": ["Suburbs"]}'`, a BitmapOr of two GIN probes |
| Two groups, AND | `attrs @> '{"d_area": ["Villages"], "c_route": ["Oppose"]}'`, one probe |
| Both of two multi-select options | `attrs @> '{"c_modes": ["Cycle", "Walk"]}'`, because array containment is a subset test |
| N/A kept as a value | `attrs @> '{"d_commute": ["N/A"]}'` |
| Not answered | `NOT (attrs ? 'd_commute')` |

`jsonb_path_ops` has no entry for `?`, so the last predicate is a filter over whatever rows the other predicates found, and on its own it's a scan of the question's rows through `(question_id, id)`. That's acceptable; ADR-004 says why.

## 6. The dashboard queries

The compiled filter is one CTE that every query below reuses. The duplicate toggle comes first: two predicates that hide answer-level and respondent-level duplicates, both dropped when the URL asks for `with=duplicates`, so a count reads either way (docs/02, section 7, decision 9). Then the three predicate kinds from docs/02 step 11, in order: containment on `attrs`, the OR'd `theme:` list on this question, and the `other:` semi-join for "what did people tagged K on question Q1 say here". Each is dropped when the URL doesn't carry it.

```sql
WITH scope AS (
  SELECT a.id, a.respondent_id, a.value_text, a.duplicate_of_answer_id
    FROM answer a
    JOIN respondent r ON r.id = a.respondent_id
   WHERE a.question_id = $q AND NOT a.is_blank
     AND a.duplicate_of_answer_id IS NULL
     AND r.duplicate_of IS NULL
     AND r.attrs @> $attr
     AND EXISTS (SELECT 1 FROM answer_theme t
                  WHERE t.answer_id = a.id AND t.theme_set_version_id = $v
                    AND t.theme_id = ANY($themes) AND t.retracted_at IS NULL)
     AND EXISTS (SELECT 1 FROM answer a2
                   JOIN answer_theme t2 ON t2.answer_id = a2.id
                  WHERE a2.respondent_id = a.respondent_id AND a2.question_id = $q1
                    AND t2.theme_set_version_id = $v1
                    AND t2.theme_id = $k AND t2.retracted_at IS NULL)
)
```

**Responses, one page.** Page numbers with OFFSET, because that's what the GOV.UK pagination component renders (docs/02, section 3.1). The cards' tags come from a second query bounded by the page's twenty ids.

```sql
SELECT id, value_text, duplicate_of_answer_id FROM scope
 ORDER BY id LIMIT 20 OFFSET 20 * ($page - 1);

SELECT at.answer_id, t.key, t.label, at.source
  FROM answer_theme at JOIN theme t ON t.id = at.theme_id
 WHERE at.answer_id = ANY($page_ids) AND at.theme_set_version_id = $v
   AND at.retracted_at IS NULL;
```

**Export, keyset.** No filter and no OFFSET: the cursor is the last id seen, and `(question_id, id)` gives an index scan with no sort.

```sql
SELECT a.id, a.respondent_id, a.value_text
  FROM answer a
 WHERE a.question_id = $q AND a.id > $after
 ORDER BY a.id LIMIT 1000;
```

**Theme counts under the filter.** The denominator is stated on screen as "of respondents who answered this question" (docs/02, screen 4), which is `count(*)` over `scope`, since one open question gives one non-blank row per respondent. The percentage is the count over that.

```sql
SELECT t.key, t.label, t.is_fallback, count(*) AS respondents,
       (SELECT count(*) FROM scope) AS denominator
  FROM scope s
  JOIN answer_theme at ON at.answer_id = s.id
   AND at.theme_set_version_id = $v AND at.retracted_at IS NULL
  JOIN theme t ON t.id = at.theme_id
 GROUP BY t.id, t.key, t.label, t.is_fallback
 ORDER BY respondents DESC;
```

**The related closed question's distribution**, among the same respondents.

```sql
SELECT o.label, count(*) AS respondents
  FROM scope s
  JOIN answer c ON c.respondent_id = s.respondent_id AND c.question_id = $related_q
  JOIN question_option o ON o.id = c.option_id
 GROUP BY o.ordinal, o.label
 ORDER BY o.ordinal;
```

The second semi-join in `scope` is the `other:` predicate on its own. The unique key on `answer` leads with `(respondent_id, question_id)`, so the inner lookup is an index probe per candidate row, and the tag check inside it goes through the full unique index on `answer_theme`. The acceptance test PR-09 will write is an `EXPLAIN (ANALYZE)` at 20,000 rows showing a Bitmap Index Scan on `respondent_attrs_gin` and an Index Scan on `answer_question_id_id` (ADR-004). The 68 ms and 218 ms that docs/01 records for this shape are from Consult's public ADR 0006, measured on an instance that isn't this one, and I haven't reproduced them.

## 7. Row counts

Assumptions: five open questions (docs/02, section 1) and twenty answer rows per respondent once multi-select is exploded and blanks are counted (ADR-004). Tags per open answer, 1.25 to 2.5, is my estimate and not a measurement; the published evaluations report agreement rates, not tags per response (docs/01, section 2), so week one measures it. Batches are 50 answers for generation and 10 for mapping (docs/02, steps 6 and 9), plus about twenty preview calls per question.

| Respondents | `respondent` | `answer` (20 each) | Open answers (5 each) | `answer_theme` at 1.25 | at 2.5 | `job_batch` (5 × (n/50 + n/10) + 100) | `job` |
|---|---|---|---|---|---|---|---|
| 1,000 | 1,000 | 20,000 | 5,000 | 6,250 | 12,500 | about 700 | 12 to 20 |
| 10,000 | 10,000 | 200,000 | 50,000 | 62,500 | 125,000 | about 6,100 | 12 to 20 |
| 100,000 | 100,000 | 2,000,000 | 500,000 | 625,000 | 1,250,000 | about 60,100 | 12 to 20 |

The table leaves out three things. Tags accumulate per version: a reopened question gets v3 and a fresh set, and the old ones stay, so ADR-004's ceiling of two and a half million tags at 100,000 respondents is the top of this range with one reopen. `job` stays at a dozen or so rows whatever the scale (stage, ingest, five `find_themes`, five `map_themes`, any previews), which is why its index barely matters. Text volume is the last: at roughly 150 tokens an open answer (docs/02, section 1) and the usual four characters a token, which I haven't checked on consultation prose, 500,000 open answers is about 300 MB of `value_text` before the tsvector, so the instance is sized from the first real consultation and not from this table.

## 8. What the proof-of-concept schema leaves out, and what it keeps

PR-03's `schema.sql` will carry fourteen of the sixteen, and its README will say so. `department` stays, cut down to `id`, `name` and `concurrent_jobs_cap`: every other table's `department_id NOT NULL REFERENCES department (id)` needs a row to point at, and the dispatch cap is one of the mechanics the tests exercise. The budget columns go, because a budget needs a monthly reconciliation against the gateway's invoice (docs/02, section 7, decision 10) and there's no invoice to reconcile. The two it drops, and why:

- `export`. The XLSX and the report go to a local path; there's no presigned link to hand out and no S3 key to record. The report renderer itself is a print view, so a row that points at it is a production concern.
- `audit_event`. Its only reader is the operator console, which is Django admin (docs/02, section 3.3), and the proof-of-concept has no Django. The retraction history it would hold is exercised through `retracted_at` and `retracted_by` instead.

Roles are the other thing the schema has to carry. `CREATE ROLE` is cluster-wide, so `schema.sql` will create the four roles (ingest, pipeline, export and `consult_admin`) inside a `DO` block guarded by a `pg_roles` lookup, then issue the `vault` and `staging` grants per database (docs/06, section 2.4). The `staging` schema itself is created by `schema.sql`; its tables are created by the `stage` job and dropped by `ingest`. The vault test in PR-06 will assert two things: a `SELECT` on `vault.respondent_identity` as the pipeline role is refused, and nothing on the pipeline path names the schema.

## 9. The two Postgres facts under the fan-in

**READ COMMITTED and `FOR UPDATE`.** A statement that blocks on a row another transaction has updated waits for that transaction, then re-evaluates its `WHERE` against the new version of that row, and only that row (docs/01, section 4, first paragraph; log row "Postgres READ COMMITTED re-evaluation and FOR UPDATE wording", checked 19 September 2026). So the guarded `UPDATE consultation` in docs/02 step 7 can't be the first statement: two finishers would each see the other's question still running in their subquery, and neither would flip. With `SELECT ... FOR UPDATE` on the consultation row first, in its own statement, the second finisher waits at the lock and its next statement's snapshot includes the first's commit. The lock is the serialiser; the guard on `status = 'processing'` is what stops a double flip.

**`NULLS NOT DISTINCT`.** By default a unique constraint treats nulls as unequal, so two rows with a null in the key never collide (docs/01, section 4; log row "Postgres NULLS NOT DISTINCT wording and default", checked 19 September 2026). The modifier is on `answer`, where a free-text row has no option, and on `job`, where `stage` and `ingest` have no question: without it two such rows never collide, `ON CONFLICT DO NOTHING` has no arbiter, and the idempotence section 3 claims for them rests on convention rather than a constraint. The outbox doesn't need it. Every outbox row names its subject (the pass, the failed job, the pause, the candidate version), the column is `NOT NULL`, and the plain key does the work (ADR-006).

## Correction, 19 September 2026

A review of PR-02 found fifteen inconsistencies across the design documents; `docs/07-reviews.md` logs the pass and PR-02b reconciles them. This file is the one rewritten in place, because PR-03 types `schema.sql` from it and a table row contradicting the DDL beneath it is the class of fault being fixed. What changed, so nothing is silent:

1. `consultation.run_id`, minted with the row and replaced by a reopen, is the pass id; every job and every milestone outbox row carries it. `notification_outbox.theme_set_version_id` is gone, `subject_id` is `NOT NULL` (a pause id and the candidate version's id give the attention and reminder rows theirs), and the key is a plain `UNIQUE (consultation_id, kind, subject_id)`. `department` gains `paused_at` and `pause_id`. Section 2 now prints the milestone insert, the reopen's two edges and the pause; section 9 no longer claims the outbox needs `NULLS NOT DISTINCT`. Was: a rule with no SQL behind it that named the version and ordered random uuids.
2. The staging table is logged and lives in a `staging` schema the pipeline role can't read (section 2, first paragraph; section 8). Was: unlogged, which Postgres truncates on crash recovery, across a human step.
3. `job.kind` gains `erasure`; `job_batch.answer_ids` gets a GIN index for erasure's trace lookup (sections 1, 2 and 4).
4. `consult_admin` is the fourth role and the only one with DELETE anywhere, the vault included; the deletion job and the console's erasure action connect as it and the worker never does (sections 1 and 8). Was: nobody held DELETE on the vault.
5. The proof-of-concept schema keeps `department` without its budget columns: fourteen tables, not thirteen (section 8).
6. The dashboard CTE carries the duplicate toggle's two predicates (section 6). Was: none.
7. `job.created_at`, `consultation.status_changed_at` and `awaiting_review_at`, for the alarms and the review-time KPI, and a partial unique index on `respondent (consultation_id, external_id)` (sections 1, 2 and 3).
