# Security and governance

The controls the design commits to, each tagged MUST, SHOULD or COULD with the reason, then where response text goes and for how long, then the governance artefacts a department would ask for before putting a consultation through the service. `docs/02-architecture.md` section 10 lists the commitments in nine lines; here they are in full. `THREAT_MODEL.md` is the adversarial view of the same controls. None of it is built yet: the proof-of-concept starts at PR-03, and where a control is to be pinned by a test the row says which pull request will do it.

## 1. Classification and where the data sits

Consultation responses are OFFICIAL under the Government Security Classifications Policy, and some will carry the SENSITIVE handling marking: a person writing about their own health or immigration status in an open answer. The policy defines OFFICIAL as information that "could cause no more than moderate damage if compromised" and says access "must be no wider than necessary" (`docs/01`, section 3; log row "GSCP"). The design treats every response as if it were at the SENSITIVE end, because the service can't tell in advance which answers are.

Inside the service two places hold response text at rest, both in London: the RDS instance and the S3 bucket (`docs/02`, section 3, the PostgreSQL and S3 rows). Inference is different. GDS's multi-region guidance says OFFICIAL, including SENSITIVE, may be processed overseas where the legal and security practices are in place, and that no transfer risk assessment is needed for a country under UK adequacy regulations (`docs/01`, section 3; log row "Multi-region cloud guidance"). So the design records the inference region on every job and lets the department's DPIA say where its data went, rather than claiming UK-only and being wrong the first time the gateway routes an alias somewhere else (`docs/02`, section 7, decision 6).

The sample material the design was drawn from is handled as `SECURITY.md` describes and stays outside the repository; the fixtures in `poc/tests/fixtures/` will describe a fictional consultation and share only the file format.

## 2. Controls

MUST is a condition of go-live for any department. SHOULD is expected in the first version and can be argued about. COULD is on the cut list in `docs/02`, section 11, and appears here so that its absence reads as a decision.

### 2.1 Data at rest and in flight

| Control | Tag | Reason |
|---|---|---|
| S3 in eu-west-2 with SSE-KMS, Block Public Access and a TLS-only bucket policy; RDS encrypted at rest, Multi-AZ, in the same region | MUST | OFFICIAL at rest in London, as `docs/02` section 3 commits. One customer-managed key per environment; per-consultation keys are in section 6 |
| Raw uploads keyed by sha256 and never read by the pipeline after ingest | MUST | The file carries the identity columns. After ingest it's the audit copy, reachable by the ingest role and an operator, and its lifecycle rule follows the consultation's `retention_until` |
| TLS between every component; the gateway reached with a token issued per environment | MUST | Nothing more to say about it |
| Inference region recorded on every job | MUST | The DPIA states where processing happened per run rather than in general (section 1) |

### 2.2 What reaches the model

| Control | Tag | Reason |
|---|---|---|
| Only the question text, the open answer and, where the question is linked, the respondent's related closed answer reach the model | MUST | The related closed answer goes because the open question's wording carries a placeholder for it (`docs/00`). Nothing else has a reason to be there |
| Demographics and identifiers never appear in a prompt | MUST | `respondent.attrs` isn't on the prompt path and the pipeline role has no grant on the vault schema (ADR-004, ADR-005). A prompt mentioning `d_area` would be a bug, and PR-07's containment tests will assert what the prompt contains |
| Regex masking of email addresses, phone numbers and UK postcodes in the copy of an open answer sent to the model; the stored answer is untouched | SHOULD | Respondents write their own details into answers. The masks catch the shapes. A name in prose gets through, and section 6 says why there's no NER |
| No tools, no function calling, no retrieval | MUST | NCSC: when an LLM processes a party's text its privileges drop to that party's (`docs/01`, section 3; log row "NCSC prompt injection"). The public write the text, so the model gets nothing to act with |
| Responses sent as JSON-encoded data after a preamble that says instructions inside them are data | MUST | The one prompt-side measure taken. It's part of the prompt contract so PR-07 can test for it with the fake model |
| Mapping batches of ten answers; generation batches of about fifty | SHOULD | Ten bounds what a single injected instruction can spoil (`docs/02`, step 9). The number is a setting; the bound is the control |

### 2.3 Model output and the human gate

| Control | Tag | Reason |
|---|---|---|
| Structured outputs requested from the gateway and the schema validated again in code | MUST | The gateway's guarantee is about shape, and shape gets checked at the boundary regardless (`docs/02`, section 10, and step 9) |
| Labels are an enum of the signed-off theme keys | MUST | A free-text label matched by string is where mis-mapping and injection both land (ADR-005) |
| Two-way id check: every answer id sent comes back exactly once, and nothing extra | MUST | A missing id is a silent drop; an extra id is the model inventing work. A failed batch retries at size one; an answer that still fails goes to `unprocessable` and the dashboard shows the count (`docs/02`, step 9). PR-07 will pin the check |
| Nothing is tagged until a named person has signed off the theme list for that question; the guard is the `UPDATE ... WHERE status = 'themes_ready'` on the server (ADR-003) | MUST | The AI Playbook's meaningful human control, section 5. PR-05 will pin the guard |

### 2.4 Tenancy and the database

| Control | Tag | Reason |
|---|---|---|
| `department_id` on every table and a mandatory scope on every query | MUST | Departments are controllers of their own responses (`docs/02`, section 1). If one department could read another's responses the service would be finished, so every query carries the scope |
| Row-level security policies keyed on the session's department | COULD | The escape hatch if scoping in code proves leaky (`docs/02`, section 7, decision 5). Switched on when the first missed scope is found |
| Parameterised SQL only; no string formatting into a query | MUST | The filter grammar in the URL compiles to parameters and never to SQL text (ADR-004). PR-09 will hand the query builders hostile filter values in a test |
| Three database roles: ingest with INSERT only on `vault.respondent_identity`; pipeline with no grant on the `vault` schema; export with SELECT on it | MUST | The role split means "identifiers never in a prompt" holds even when the code gets it wrong, because the pipeline role can't read the vault (ADR-004). PR-03's `schema.sql` will create the roles; PR-06 will connect as the pipeline role and expect a permission error |
| Tags never deleted; retraction sets `retracted_at` and writes an `audit_event` | MUST | The history of who tagged what, and the guard against a resumed model run resurrecting a retracted tag (ADR-004). PR-05 will pin the resurrection case |

### 2.5 Logs, traces and errors

| Control | Tag | Reason |
|---|---|---|
| Log lines carry ids, counts, durations and error codes. Never answer text, theme text or a prompt | MUST | `docs/02`, section 3.4. CloudWatch is read by more people than the database is, and log lines get pasted into tickets |
| `job.error_code` and `job.provider_request_id` (docs/04) are all a failed job stores; there is no column for a message body | MUST | Provider error messages can echo the prompt back. PR-03 will pin both columns with a test |
| Sentry with `send_default_pii=False` and a before-send scrubber that drops request bodies and local variables | MUST | An uncaught exception in the mapping loop has answer text in its frame. Without the scrubber that stack trace lands in Sentry with the answer text in it |
| Langfuse holds prompt and completion text on the gateway side inside the OFFICIAL boundary, retention aligned to `retention_until`, readable by the platform team only | MUST | The deliberate exception to ids-only (`docs/02`, section 3.4). NCSC's advice is to log the LLM's full input and output (`docs/01`, section 3), and debugging a mis-tagged batch needs it. If the platform team can't give the boundary and retention assurance, tracing drops to metadata (ADR-005) |
| Correlation ids (consultation, question, job, run) on every line | SHOULD | What makes an ids-only log worth reading. Without them the rule above just makes the logs quiet |

### 2.6 Retention and erasure

| Control | Tag | Reason |
|---|---|---|
| `retention_until` on every consultation, set at Confirm from the department's own schedule | MUST | The date drives the S3 lifecycle rule, the deletion job and the trace retention (section 4) |
| A deletion job that removes a consultation's rows, its S3 objects and its traces after `retention_until`, and writes an `audit_event` saying it did | MUST | A retention date on its own deletes nothing. The job does the deleting, and the audit event is what the department can point to afterwards |
| Erasure of one respondent on request, from the operator console, without a database engineer | MUST | Section 4 has the steps. The department is obliged to honour the request, so the platform has to be able to carry it out without a database engineer |

### 2.7 Secrets, uploads and exports

| Control | Tag | Reason |
|---|---|---|
| Model keys live in the gateway. The app holds one gateway token per environment from a secrets manager, injected at task start; nothing in the image, the repository or an environment file | MUST | A key in the app is a key in every log and every core dump (ADR-005). Rotation is a runbook item |
| Upload guards: a size cap on the presigned URL and again at stage; a decompression-ratio ceiling before an XLSX is opened; `defusedxml` for the XML inside it; CSV rows and cells capped in length | MUST | An XLSX is a zip of XML, so a zip bomb and an entity-expansion attack arrive in one file. The caps are settings, and my starting points are estimates: a few hundred megabytes on the file (the largest consultation in `docs/01`, section 5, is about 37 million words, on the order of 200 MB of text at an assumed five to six bytes a word) and a ratio ceiling of 100:1, which ordinary spreadsheets sit well under. PR-04 will pin the guards |
| Export cells are written as text cells, never as formula cells, and a value beginning with `=`, `+`, `-`, `@`, tab or carriage return gets a neutralising prefix so it stays text through a round trip via CSV. A lone `-`, the file's own marker for no answer (`docs/00`), is left alone | MUST | An answer of `=HYPERLINK(...)` becomes a live formula the moment the department opens the export. PR-09 will pin it |
| Presigned download links for exports live for minutes | SHOULD | A link in an email thread is a link forwarded. The lifetime is a setting |
| The SQS queue's policy allows `SendMessage` from the reconciler's task role only; the worker role can receive and delete | SHOULD | The message is a hint and the claim is conditional (`docs/02`, step 5), so this bounds nuisance rather than damage |

### 2.8 Who can do what

Authentication is upstream. What the service enforces is this table, and the operator rows are the ones an audit will ask about.

| Who | Can | Can't |
|---|---|---|
| A department user | Upload, configure, review, sign off, explore and export within their department | See another department's consultations; read the vault; delete a tag (retraction only) |
| An operator (Django admin) | Retry a job, reassign a question, pause a department, resend an outbox row, run an erasure; every action writes an `audit_event` with actor, before and after (`docs/02`, section 3.3) | Edit a theme-set version or a tag; read the vault |
| The pipeline role | Read answers and question text; insert tags, batches and versions | Touch the `vault` schema at all |
| The ingest role | INSERT into `vault.respondent_identity` | SELECT from it |
| The export role | SELECT from the vault, to put identity columns back into the department's spreadsheet | Anything on the pipeline path |
| The platform team | Read Langfuse traces; operate the database and the gateway token | Not much. That's the insider case `THREAT_MODEL.md` leaves out, and says so |

## 3. Where response text goes

For anyone filling in a DPIA. "Response text" means an open answer; the demographic and closed answers are listed where they go too.

| Store | Holds response text | Region | Retention | Who can read it |
|---|---|---|---|---|
| Postgres (`answer.value_text`, `theme` labels and descriptions, `respondent.attrs`) | Yes, and every demographic and closed answer | London | To `retention_until`, then the deletion job | Department users under scoping; the platform team |
| `vault.respondent_identity` | Identity columns only | London | Same | The export role. Nothing on the pipeline path |
| S3, raw upload | Yes, with the identity columns | London | Lifecycle rule from `retention_until` | The ingest role and an operator |
| S3, exports | Yes, with themes | London | Same lifecycle; regenerated after an erasure | Department users through a short-lived link |
| The gateway and Langfuse | Prompts and completions: question text, open answers, related closed answers, theme labels | Gateway side, inside the OFFICIAL boundary | Aligned to `retention_until` | The platform team |
| The model provider | In flight, per call | Recorded per job | The provider's terms, named in the DPIA pack | The provider, under those terms |
| CloudWatch | No | London | The log group's retention | Engineers and operators |
| Sentry | No, given the scrubber | Wherever the instance is; a self-hosted one keeps the question short | Sentry's retention | Engineers |
| GOV.UK Notify | No: consultation name, status and a link | UK and Ireland | Message content seven days by default (`docs/01`, section 3; log row "Notify: UK and Ireland AWS") | The recipient |
| `job.error` | No: a code and a request id | London | With the job row | Operators |

## 4. Retention and erasure

`retention_until` is a date on the consultation, set at Confirm. The department chooses it against its own retention schedule; the Consultation Principles' twelve-week publication window (`docs/01`, section 3; from notes, to re-check) is the least time the data has to be around. Three things read the date: the S3 lifecycle rule on the raw upload and the exports, the gateway's trace retention, and a deletion job that runs daily, deletes the consultation's rows, and writes an `audit_event` with counts.

Erasure of one respondent is a different shape, done on request while the consultation is live. It's an operator action in the console and one transaction per respondent:

1. Find the respondent, by identity through the export role's view of the vault, or by their respondent id (`respondent_ref` in the wireframes).
2. Re-point any `answer.duplicate_of_answer_id` and `respondent.duplicate_of` that name the rows about to go at the next-oldest member of that cluster, or null them if there is none; the cluster's count on the dashboard drops by one, its canonical row does.
3. Delete their `theme_example` rows, their `answer_theme` rows and their `answer` rows, in that order because of the foreign keys in docs/04. This is the one place a tag row is deleted rather than retracted, and the `audit_event` carries the ids and the count.
4. Delete the vault row, then the `respondent` row and `attrs` with it; the vault row first because it references the respondent.
5. Quotes shown on the sign-off screen and in the report are stored by answer id, so they go with the answer. A theme's description was written by the model from many answers and isn't reverted; the audit event notes which theme-set versions the answer contributed to.
6. The batch that carried the answer is known from `job_batch.answer_ids` (ADR-002), so the trace for that batch can be found by id. `job_batch.trace_id` (docs/04, section 2) is the gateway's id for that call, and the deletion request to Langfuse is keyed by it.
7. The raw upload is the awkward one. Its sha256 is its identity, so it can't be edited in place. The erasure writes a redacted copy under a new key, records both keys on the audit event, and deletes the original. `consultation.upload_sha256` is updated to the redacted copy's hash in the same transaction, so docs/04's audit-copy rule (the S3 object keyed by that hash plus `source_row_no`) still points at an object that exists.
8. Regenerate the exports as an ordinary `export` job. Old export objects are removed.

Exports already downloaded are outside the service. The overview screen shows who last exported and when (`docs/02`, section 12, screen 5), so the department knows who to ask, and the recall is theirs to do as controller.

## 5. Governance

| Artefact | Whose | What the service provides |
|---|---|---|
| A processor agreement | The platform team, once; each department signs | Departments are controllers and the platform is a processor (`docs/02`, section 1). One document, with the sub-processors named: AWS in London, the gateway's provider per alias, GOV.UK Notify |
| A DPIA | Each department, per consultation or per programme. The pilot's ATRS record says the same (`docs/01`, section 2) | The manifest export pre-fills it: model alias, inference region per job, prompt hash, theme-set versions, run ids, the agreement rate from human edits, exclusions, the retention date (`docs/02`, step 12). Section 3 above is the "where does it go" table |
| An ATRS record | The department that owns the service in production | The mandatory scope covers tools with "a significant influence on a decision-making process with public effect", published once at beta, pilot or production (`docs/01`, section 3; log row "ATRS mandatory scope"). I read the first criterion as met. The pilot's public record is at the pre-deployment phase, so a production service publishes its own, and the manifest fields feed it |
| A methodology note | Published with the service | What the model does and doesn't do, in plain English, with the correction path (below) |
| An accessibility statement | The service owner | Required by the accessibility regulations, which cover intranets (`docs/01`, section 3; log row "Accessibility regulations") |

**Meaningful human control.** The AI Playbook's principle 4 says humans validate high-risk decisions influenced by AI, that the product is fully tested before deployment, and that regular checks of the live tool are in place (`docs/01`, section 3; log row "AI Playbook"). The per-question sign-off by a named person is the validation. The DWP evaluation's figure of 73% of mappings left unchanged (`docs/01`, section 2; log row "DWP evaluation") means a reviewer changes roughly one mapping in four, which is the case for the gate that docs/02 section 7 (decision 4) already makes. The replay of the piloted consultations (ADR-007) is the test before deployment, and the edit rate, the preview's Other rate and the agreement rate on the overview are the regular checks.

**Methodology note and correction.** A member of the public who finds their response in a published summary under a theme they don't recognise needs somewhere to go. The note says how themes were proposed, who confirmed them, and how tags can be corrected; the per-response correction on the dashboard (`docs/02`, step 11) with its `audit_event` is the path. A correction never edits a signed-off version: it's a retraction and a new tag with `source = 'human'`.

**Counting both ways.** The Consultation Principles (2018; `docs/01`, section 3; from notes, to re-check) expect a published summary of responses, and a summary has to say how many people said what. A campaign is both: the water commission's call for evidence took 15,741 responses through one campaign and 28,458 through another (`docs/01`, section 5; log row "IWC"). That's a lot of responses and a small number of views. So exact duplicates are flagged at answer level and at respondent level, counted with and without, toggled on the dashboard, and never removed (`docs/02`, section 7, decision 9).

**Welsh.** The public in Wales may respond in Welsh and a consultation covering Wales has to handle those responses no less favourably than English ones (`docs/01`, section 3; log row "Welsh-language responses"; from notes, to re-check). Which regime applies to a given department, a Welsh language scheme or the Welsh Language Standards made under the Welsh Language (Wales) Measure 2011, is a question for that department; the Measure isn't in the `docs/01` log yet and goes in when the 2019 guidance row is re-checked. The model's ability to theme Welsh text hasn't been evaluated by anyone whose evaluation I can cite, so a Welsh answer is routed to a human-read lane until it has. The lane is a dated gap on the cut list (`docs/02`, section 11), and detecting Welsh text at ingest is part of the same gap. It isn't a permanent exclusion.

**The Service Standard and WCAG 2.2 AA.** A cross-government service gets a service owner, user research on the sign-off screen, and an assessment (`docs/02`, section 10; ADR-007). Accessibility is assessed on the service rather than the component library: GOV.UK Frontend's own statement says the codebase meets WCAG 2.2 AA (`docs/01`, section 3; log row "GOV.UK Frontend"), which says nothing about a filter change on the dashboard being announced to a screen reader, or a non-visual route through a 200-row theme table. Those two are the acceptance criteria in `docs/02`, section 3.1, tested with people who use assistive technology.

## 6. Out of scope, and why

| Item | Why not now | What stands in |
|---|---|---|
| Authentication, sessions, single sign-on | Solved upstream; every request arrives with a user and a department (`docs/02`, section 1) | Authorisation is in scope: `department_id` scoping on every query, and the operator console's audit trail |
| Per-consultation KMS keys | What they buy is crypto-shredding, and a key per consultation is a key-management job at 600 consultations a year (`docs/02`, section 1). The deletion job and the erasure path do the deleting | One customer-managed key per environment. Revisit if a department's contract asks for it |
| PII named-entity recognition on open answers | A second model to run, tune and evaluate before the first theme, with its own false negatives to explain | Identity columns go to the vault at configure time; regex masking on the prompt path catches the obvious shapes; the DPIA says names written into prose reach the model |
| Row-level security | Listed in section 2.4 as COULD. Two enforcement points for one rule, until the first is shown to leak | Scoping in code, to be pinned by tests from PR-03 |
| Resistance to prompt injection | NCSC's view is that it may never be properly mitigated (`docs/01`, section 3), and I've no reason to think this design is the exception | Containment: no tools, data after a preamble, the enum, the two-way check, batches of ten, and a reviewer who sees the counts |
| Bot and campaign authenticity | Whether 90,835 of 118,756 responses came from automated programmes (`docs/01`, section 5; log row "Tobacco and vapes") is a judgement the department makes and publishes | Duplicate flags at both levels, the validator's counts before spend, and the cost estimate on the Confirm screen |

## 7. See also

`THREAT_MODEL.md` for the same controls seen from the attacker's side and the logging policy in five lines. `docs/04` for the tables and roles the controls in section 2.4 depend on. PR-03 for the first two tests: `job.error` and the log formatter.

## Correction, 19 September 2026

A review of PR-02 found fifteen inconsistencies across the design documents; `docs/07-reviews.md` logs the pass and PR-02b reconciles them. The entries below correct this file. Each names the section or row it corrects and gives the corrected text; the body above is left as it merged.

1. **Section 4, erasure of one respondent.** Steps 6 to 8 are external effects (a Langfuse delete, an S3 rewrite, export jobs) and can't sit inside "one transaction per respondent". The transaction is steps 1 to 5, an `erasure` job row (`docs/04`, `job.kind`) whose `params` carry the respondent id, the answer ids, `source_row_no` and the upload's key, and the `audit_event`. The job does the rest with the retry budget every job has (`failed_retryable`, backoff, five attempts) and an attention outbox row if it still fails, so a Langfuse outage delays an erasure and never loses one. Steps 6 to 8 as the job runs them:

   6. An answer sits in several `job_batch` rows (generation, preview, mapping) and a duplicate in none. The job deletes every trace whose batch carried the answer: `SELECT trace_id FROM job_batch WHERE answer_ids @> ARRAY[$answer_id]::bigint[]`, through the GIN index on `answer_ids` (`docs/04`, section 4). For a duplicate respondent there is no trace to delete, and the honest statement is that their text, identical to the canonical answer's, stays in the canonical's traces until the canonical is erased. The audit event says so.
   7. The raw upload: write the redacted copy under a new key, update `consultation.upload_sha256` to its hash, then delete the original, in that order, so the audit-copy rule in `docs/04` never points at an object that has gone. Both keys go on the audit event.
   8. Regenerate the exports as ordinary `export` jobs and remove the old objects.

2. **Section 2.4, the roles row.** Three roles left nobody holding DELETE on the vault, so the deletion job and an erasure couldn't run. Four roles, and the row reads:

   | Control | Tag | Reason |
   |---|---|---|
   | Four database roles: ingest with INSERT only on `vault.respondent_identity` and ownership of the `staging` schema; pipeline with no grant on `vault` or `staging`; export with SELECT on the vault; `consult_admin` with DELETE on `vault.respondent_identity`, `respondent`, `answer`, `answer_theme` and `theme_example`, used only by the deletion job, the erasure job and the operator console's erasure action | MUST | Nobody else holds DELETE on the vault or the answer tables, so the deleting that retention and erasure need is a role grant and not a database engineer, and the pipeline role still can't read the vault (ADR-004). PR-03's `schema.sql` will create the four roles; PR-06 will connect as the pipeline role and expect a permission error |

3. **Section 2.8, the operator row, and a new row.** Erasure step 1 finds a respondent by identity through the vault, which the table said an operator can't do. The lookup is the one stated exception: it runs as `consult_admin` and writes an `audit_event` every time it runs, found or not.

   | Who | Can | Can't |
   |---|---|---|
   | An operator (Django admin) | Retry a job, reassign a question, pause a department, resend an outbox row, run an erasure; every action writes an `audit_event` with actor, before and after (`docs/02`, section 3.3) | Edit a theme-set version or a tag; read the vault, except through the erasure action's lookup, which runs as `consult_admin` and is audited whether or not it finds anyone |
   | The admin role (`consult_admin`) | DELETE on the vault and the answer tables, for the deletion job, the erasure job and the console's erasure lookup | Anything on the pipeline path; insert a tag, a batch or a version |

4. **Section 3, the vault row.** Who can read it: the export role, and `consult_admin` for an audited erasure lookup. Nothing on the pipeline path.

5. **Section 4, step 1.** "By identity through the export role's view of the vault" reads: by identity through the vault as `consult_admin`, which writes an `audit_event` for the lookup itself, or by their respondent id.
