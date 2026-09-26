-- The proof-of-concept schema, typed from docs/04-data-model.md. Fourteen of
-- the design's sixteen tables (section 8 says which two stay out and why),
-- the vault and staging schemas, and the four roles from docs/06 section 2.4.
--
-- Applied whole by `consult init`. Every statement is IF NOT EXISTS so a
-- second run on a live database is a no-op; `consult init --reset` drops the
-- lot first (store.reset). There is no migration tool: a proof-of-concept
-- changes its schema by rewriting this file (plans/PR-03, section 2).
--
-- Two conventions from docs/04. Every table carries department_id, because
-- departments are controllers and every query goes through a mandatory scope
-- (docs/02, section 10). Statuses are text with a CHECK, not enum types, so
-- adding a state is a constraint change and never an enum value nobody can
-- drop.

CREATE SCHEMA IF NOT EXISTS vault;
-- The stage job's per-upload tables live here (docs/04, section 2). Logged,
-- not UNLOGGED, because they outlive the human configure step and Postgres
-- truncates an unlogged table after a crash (docs/01, section 6).
CREATE SCHEMA IF NOT EXISTS staging;

-- Cut down to the three columns the tests exercise (docs/04, section 8): the
-- dispatch cap is a mechanic; the budget needs an invoice to reconcile against.
CREATE TABLE IF NOT EXISTS department (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- One department per name: the command line finds or creates by it, and
  -- two runs racing on a new name have to land on one row (docs/04,
  -- section 3 as corrected).
  name                text NOT NULL UNIQUE,
  concurrent_jobs_cap integer NOT NULL DEFAULT 6 CHECK (concurrent_jobs_cap > 0)
);

CREATE TABLE IF NOT EXISTS consultation (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  department_id      uuid NOT NULL REFERENCES department (id),
  name               text NOT NULL,
  source             text NOT NULL CHECK (source IN ('citizen_space', 'qualtrics', 'generic')),
  status             text NOT NULL DEFAULT 'draft' CHECK (status IN
                       ('draft', 'staging', 'staged', 'processing', 'awaiting_review', 'ready')),
  attention_reason   text,
  -- The pass id: minted with the row, copied onto every job the pass inserts,
  -- replaced by a reopen (docs/04, section 2).
  run_id             uuid NOT NULL DEFAULT gen_random_uuid(),
  status_changed_at  timestamptz NOT NULL DEFAULT now(),
  awaiting_review_at timestamptz,
  model_alias        text,
  -- Five years at the most: the S3 lifecycle backstop in docs/06 expires
  -- objects at five years whatever this says.
  retention_until    date,
  created_by         uuid NOT NULL,
  upload_sha256      bytea,
  row_count          integer,
  column_roles       jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS question (
  id                         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  department_id              uuid NOT NULL REFERENCES department (id),
  consultation_id            uuid NOT NULL REFERENCES consultation (id),
  column_ref                 text NOT NULL,
  question_text              text NOT NULL,
  kind                       text NOT NULL CHECK (kind IN ('demographic', 'closed', 'open', 'identity')),
  response_type              text CHECK (response_type IN ('single_select', 'likert_5', 'multi_select')),
  ordinal                    integer NOT NULL,
  related_closed_question_id uuid REFERENCES question (id),
  value_policy               jsonb NOT NULL DEFAULT '{}',
  -- The per-question state machine (docs/02, section 6); null unless kind is open.
  status                     text CHECK (status IN ('configured', 'finding_themes', 'themes_ready',
                               'signed_off', 'assigning_themes', 'complete', 'find_failed', 'map_failed')),
  assigned_to                uuid,
  review_started_at          timestamptz,
  -- Saving the configure screen twice upserts a column rather than duplicating it.
  UNIQUE (consultation_id, column_ref)
);

-- Options are rows, so a label can contain a comma (docs/00).
CREATE TABLE IF NOT EXISTS question_option (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  department_id uuid NOT NULL REFERENCES department (id),
  question_id   uuid NOT NULL REFERENCES question (id),
  label         text NOT NULL,
  ordinal       integer NOT NULL,
  UNIQUE (question_id, label)
);

CREATE TABLE IF NOT EXISTS respondent (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  department_id   uuid NOT NULL REFERENCES department (id),
  consultation_id uuid NOT NULL REFERENCES consultation (id),
  external_id     text,
  source_row_no   integer NOT NULL,
  -- The filter read model: every demographic and closed answer, keyed by
  -- column, values always arrays (docs/04, section 5). Never on the prompt path.
  attrs           jsonb NOT NULL DEFAULT '{}',
  duplicate_of    bigint REFERENCES respondent (id),
  -- A second delivery of the ingest message finds the rows already there.
  UNIQUE (consultation_id, source_row_no)
);
CREATE INDEX IF NOT EXISTS respondent_attrs_gin ON respondent USING gin (attrs jsonb_path_ops);
-- Two file rows with one respondent id are one person twice or a broken
-- export; ingest refuses it rather than guessing (docs/04, section 3).
CREATE UNIQUE INDEX IF NOT EXISTS respondent_external_id ON respondent (consultation_id, external_id)
  WHERE external_id IS NOT NULL;

-- Identity columns, in their own schema so the grants can differ: the ingest
-- role inserts, the export role selects, the pipeline role has no grant at
-- all, and only consult_admin deletes (docs/06, section 2.4 as corrected).
CREATE TABLE IF NOT EXISTS vault.respondent_identity (
  department_id uuid NOT NULL REFERENCES department (id),
  respondent_id bigint NOT NULL REFERENCES respondent (id),
  column_ref    text NOT NULL,
  value_text    text NOT NULL,
  PRIMARY KEY (respondent_id, column_ref)
);

-- The long table: one row per respondent per question, a multi-select answer
-- one row per chosen option, a not-answered cell a row with is_blank set so
-- the denominator can be counted (ADR-004).
CREATE TABLE IF NOT EXISTS answer (
  id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  department_id          uuid NOT NULL REFERENCES department (id),
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
  -- NULLS NOT DISTINCT is what makes a free-text row, whose option_id is
  -- null, collide with itself; by default nulls never do (docs/04, section 9).
  UNIQUE NULLS NOT DISTINCT (respondent_id, question_id, option_id)
);
-- The per-question scan under every dashboard query and the keyset cursor for export.
CREATE INDEX IF NOT EXISTS answer_question_id_id ON answer (question_id, id);
-- duplicate_of_answer_id at ingest, and the campaign toggle's count of identical answers.
CREATE INDEX IF NOT EXISTS answer_question_sha   ON answer (question_id, text_sha256);
CREATE INDEX IF NOT EXISTS answer_tsv_gin        ON answer USING gin (tsv);

-- An immutable list: v1 the candidate, v2 the signed-off copy, v3 a reopen
-- (ADR-004). edit_version is the counter the sign-off screen's
-- expected_version checks; version_no is lineage.
CREATE TABLE IF NOT EXISTS theme_set_version (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  department_id     uuid NOT NULL REFERENCES department (id),
  question_id       uuid NOT NULL REFERENCES question (id),
  version_no        integer NOT NULL,
  status            text NOT NULL CHECK (status IN ('candidate', 'signed_off', 'superseded')),
  parent_version_id uuid REFERENCES theme_set_version (id),
  edit_version      integer NOT NULL DEFAULT 0,
  signed_off_by     uuid,
  signed_off_at     timestamptz,
  -- A find_themes job delivered twice can't write a second v1; two reviewers
  -- confirming can't freeze two v2s.
  UNIQUE (question_id, version_no)
);

-- One theme in one version. key is the enum value the model returns.
CREATE TABLE IF NOT EXISTS theme (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  department_id        uuid NOT NULL REFERENCES department (id),
  theme_set_version_id uuid NOT NULL REFERENCES theme_set_version (id),
  key                  text NOT NULL,
  label                text NOT NULL,
  description          text,
  is_longlist          boolean NOT NULL DEFAULT false,
  is_fallback          boolean NOT NULL DEFAULT false,
  lineage_theme_id     uuid REFERENCES theme (id),
  preview_count        integer,
  -- The enum has one meaning per key, and a re-run of condensation writes each key once.
  UNIQUE (theme_set_version_id, key)
);

-- The quotes on the sign-off screen, from the 200-answer preview.
CREATE TABLE IF NOT EXISTS theme_example (
  department_id uuid NOT NULL REFERENCES department (id),
  theme_id      uuid NOT NULL REFERENCES theme (id),
  answer_id     bigint NOT NULL REFERENCES answer (id),
  rank          integer NOT NULL,
  -- A re-run preview keeps one quote per answer per theme.
  PRIMARY KEY (theme_id, answer_id)
);

-- The unit of work and the ledger. claimed_by, attempts and heartbeat_at are
-- the lease and the fence (docs/02, step 5). error_code and
-- provider_request_id are all a failure stores: every column that can hold
-- a string is on the allow-list a test holds the table to, and params is
-- held to a JSON object so a bare string can't land there either
-- (THREAT_MODEL.md, section 2).
CREATE TABLE IF NOT EXISTS job (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  department_id       uuid NOT NULL REFERENCES department (id),
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
  params              jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(params) = 'object'),
  tokens_in           bigint NOT NULL DEFAULT 0,
  tokens_cached       bigint NOT NULL DEFAULT 0,
  tokens_out          bigint NOT NULL DEFAULT 0,
  cost_pence          integer NOT NULL DEFAULT 0,
  -- The vocabulary in consult/errors.py; a test holds the two lists equal
  -- in both directions. A message body here is refused, not just discouraged.
  error_code          text CHECK (error_code IN ('gateway_timeout', 'gateway_rate_limited',
                        'gateway_unavailable', 'gateway_rejected', 'model_output_invalid',
                        'lease_lost', 'input_invalid', 'worker_error')),
  provider_request_id text
);
-- One job per (consultation, open question, kind, run), enforced by the index
-- so a code path that forgets the rule gets a conflict and not a second job.
-- question_id is null for stage and ingest, hence the modifier; previews,
-- exports and erasures repeat by design, hence the partial (docs/04, section 2).
CREATE UNIQUE INDEX IF NOT EXISTS job_one_per_run ON job (consultation_id, question_id, kind, run_id)
  NULLS NOT DISTINCT WHERE kind IN ('stage', 'ingest', 'find_themes', 'map_themes');
-- The reconciler's five scans.
CREATE INDEX IF NOT EXISTS job_by_status ON job (status);

-- One checkpoint: one call of one stage function on one chunk (ADR-002). A
-- worker taking over reads the last finished batch and starts at the next.
-- department_id is here as on every table (docs/04's stated convention),
-- though its DDL sketch left it off this one.
CREATE TABLE IF NOT EXISTS job_batch (
  department_id uuid NOT NULL REFERENCES department (id),
  job_id        uuid NOT NULL REFERENCES job (id),
  batch_no      integer NOT NULL,
  stage         text NOT NULL,
  answer_ids    bigint[] NOT NULL,
  status        text NOT NULL CHECK (status IN ('done', 'unprocessable')),
  trace_id      text,
  tokens_in     integer NOT NULL DEFAULT 0,
  tokens_out    integer NOT NULL DEFAULT 0,
  finished_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, batch_no)
);
-- Erasure's trace lookup: every batch that carried an answer (docs/06, section 4).
CREATE INDEX IF NOT EXISTS job_batch_answer_ids_gin ON job_batch USING gin (answer_ids);

-- A tag. Never deleted; retracted in place by setting retracted_at. The unique
-- index is full, not partial on live rows, so a resumed model run's ON
-- CONFLICT DO NOTHING lands on the retracted row and can't resurrect it (ADR-004).
CREATE TABLE IF NOT EXISTS answer_theme (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  department_id        uuid NOT NULL REFERENCES department (id),
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
-- Theme counts under a filter read live rows only.
CREATE INDEX IF NOT EXISTS answer_theme_live ON answer_theme (theme_set_version_id, theme_id, answer_id)
  WHERE retracted_at IS NULL;

-- The email, written in the transition's commit (ADR-006). Every row names
-- its subject (the pass, the failed job, the pause, the candidate version),
-- so the key is plain and needs no NULLS NOT DISTINCT.
CREATE TABLE IF NOT EXISTS notification_outbox (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  department_id   uuid NOT NULL REFERENCES department (id),
  consultation_id uuid NOT NULL REFERENCES consultation (id),
  kind            text NOT NULL CHECK (kind IN
                    ('themes_ready', 'analysis_ready', 'attention_needed', 'review_reminder')),
  subject_id      uuid NOT NULL,
  status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sending', 'sent')),
  notify_id       text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  sent_at         timestamptz,
  -- One email per milestone per pass.
  UNIQUE (consultation_id, kind, subject_id)
);
-- The relay's ORDER BY id FOR UPDATE SKIP LOCKED.
CREATE INDEX IF NOT EXISTS notification_outbox_pending ON notification_outbox (id) WHERE status = 'pending';

-- The four roles (docs/06, section 2.4 as corrected). CREATE ROLE is
-- cluster-wide and has no IF NOT EXISTS, so each is guarded by a pg_roles
-- lookup; the tests apply this file to a fresh database each session and the
-- roles are already there from the last one. Two sessions initialising a
-- fresh cluster at once can both pass a lookup, and the second's CREATE ROLE
-- then fails on pg_authid's unique index; the handler on each lets it lose
-- that race harmlessly, since the role it wanted now exists. NOLOGIN: a
-- connection is made as the login user and SET ROLE picks the grant set,
-- which is how PR-06's test will connect as the pipeline role and expect the
-- vault to refuse it.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'consult_ingest') THEN
    BEGIN
      CREATE ROLE consult_ingest NOLOGIN;
    EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL;
    END;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'consult_pipeline') THEN
    BEGIN
      CREATE ROLE consult_pipeline NOLOGIN;
    EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL;
    END;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'consult_export') THEN
    BEGIN
      CREATE ROLE consult_export NOLOGIN;
    EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL;
    END;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'consult_admin') THEN
    BEGIN
      CREATE ROLE consult_admin NOLOGIN;
    EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL;
    END;
  END IF;
END
$$;

-- Grants are per database and follow the roles. Nobody but consult_admin
-- holds DELETE anywhere: tags are retracted, jobs and batches are kept,
-- staging tables are dropped by the role that owns them.
GRANT USAGE ON SCHEMA public TO consult_ingest, consult_pipeline, consult_export, consult_admin;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO consult_ingest, consult_pipeline;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO consult_ingest, consult_pipeline;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO consult_export;
GRANT SELECT, DELETE ON ALL TABLES IN SCHEMA public TO consult_admin;
-- The erasure transaction is steps 1 to 5 of docs/06 section 4 as corrected:
-- it inserts the erasure job row, re-points duplicate_of_answer_id and
-- duplicate_of at the next-oldest member of a cluster, and deletes. The
-- redacted upload's hash is the job's step 7, written under the pipeline
-- role's UPDATE, so consult_admin needs no UPDATE on consultation.
GRANT INSERT ON job TO consult_admin;
GRANT UPDATE ON respondent, answer TO consult_admin;

-- The vault: insert-only for ingest, read-only for export, delete for admin,
-- and no grant at all for the pipeline, which is what keeps "identifiers
-- never in a prompt" true even when the code gets it wrong.
GRANT USAGE ON SCHEMA vault TO consult_ingest, consult_export, consult_admin;
GRANT INSERT ON vault.respondent_identity TO consult_ingest;
GRANT SELECT ON vault.respondent_identity TO consult_export;
GRANT SELECT, DELETE ON vault.respondent_identity TO consult_admin;

-- Staging: the ingest role creates a per-upload table at stage and drops it
-- at ingest; the pipeline role can't read it because identity columns sit
-- there until ingest moves them to the vault.
GRANT ALL ON SCHEMA staging TO consult_ingest;
