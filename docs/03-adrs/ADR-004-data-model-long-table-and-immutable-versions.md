# ADR-004: A long answer table, a read model for filters, and versions that never change

## Status

Accepted, 19 September 2026.

## Context

A responses file arrives as one row per respondent and one column per question, with multi-select answers comma-joined and demographics as plain columns (the format is described in docs/00-brief-and-data-shape.md). The dashboard has to filter one question's answers by demographic, by closed answer, by theme on this question and by theme on another question, at up to 100,000 respondents and seventy questions (assumptions in docs/02-architecture.md). Themes are edited by people after the model proposes them and tags are edited after the model assigns them, and a report has to say which version of which list produced every number in it.

## Decision

**Answers are long.** `answer(id, consultation_id, respondent_id, question_id, option_id, value_text, is_blank, text_sha256, duplicate_of_answer_id, tsv)`, one row per respondent per question, and for multi-select one row per chosen option. The uniqueness is `UNIQUE NULLS NOT DISTINCT (respondent_id, question_id, option_id)`, so a free-text answer with a null option is unique too; without the modifier nulls never collide (PostgreSQL 17 manual, CREATE TABLE, checked 19 September 2026). A B-tree on `(question_id, id)` serves keyset paging.

**Filters read a write-once document.** `respondent.attrs` is a jsonb map of every demographic and every closed answer keyed by column reference, with values always arrays so single-select and multi-select use the same containment test. It's built once at ingest from the answer rows and never edited by hand. The index is GIN with `jsonb_path_ops`, which supports only `@>` (and the jsonpath operators) but is "usually much smaller" with better specificity than the default class (PostgreSQL 17 manual, section 8.14.4, checked 19 September 2026). A "not answered" facet compiles to a negated key test, which no index helps with; it's a filter within the question's rows and that's fine.

The filter query, said aloud, for "answers to question Q from respondents matching an attribute, tagged with one of these themes here, and tagged with theme K on question Q1":

```sql
SELECT a.id, a.value_text
  FROM answer a
  JOIN respondent r ON r.id = a.respondent_id
 WHERE a.question_id = $q
   AND r.attrs @> $attr            -- e.g. {"<column_ref>": ["<value>"]}
   AND EXISTS (SELECT 1 FROM answer_theme t
                WHERE t.answer_id = a.id AND t.theme_set_version_id = $v
                  AND t.theme_id = ANY($themes) AND t.retracted_at IS NULL)
   AND EXISTS (SELECT 1 FROM answer a2
                JOIN answer_theme t2 ON t2.answer_id = a2.id
                WHERE a2.respondent_id = r.id AND a2.question_id = $q1
                  AND t2.theme_id = $k AND t2.retracted_at IS NULL)
 ORDER BY a.id
 LIMIT 20 OFFSET $offset;
```

`OFFSET` for the dashboard, because the GOV.UK pagination component shows page numbers and I don't expect anyone to page past 200 on a screen; keyset on `(question_id, id)` for exports and deep scroll, where they would.

**Theme lists are versions.** `theme_set_version(id, question_id, version_no, status candidate | signed_off | superseded, parent_version_id, signed_off_by, signed_off_at)` with `UNIQUE (question_id, version_no)`. `find_themes` writes v1 as a candidate, sign-off freezes v2 (ADR-003), a reopened consultation gets v3 on whatever model alias is current. Nothing in a signed-off version is edited in place.

**Tags are never deleted.** `answer_theme(id, answer_id, theme_id, theme_set_version_id, job_id, batch_id, source ai | human, user_id, created_at, retracted_at, retracted_by)` with a full unique index on `(answer_id, theme_id, theme_set_version_id)` and a partial index on live rows for the counts. Workers insert with `ON CONFLICT DO NOTHING`. A person retracting a tag sets `retracted_at`; re-adding clears it; the audit event is the history. The full index, rather than a partial one on live rows, is what stops a resumed model run from re-inserting a tag a reviewer has retracted: the conflict lands on the retracted row and does nothing.

**Identity is elsewhere.** Columns the configure step marks as identity (name, email, postcode) go to `vault.respondent_identity` at ingest through an insert-only role. The pipeline role has no grant on that schema; the export role has SELECT. There's no `raw_row` column, because it would carry identity past the vault; the original file in S3, keyed by its sha256, plus `source_row_no` is the audit copy. Exact duplicates are flagged at answer level (`duplicate_of_answer_id`, same question, identical normalised text) and respondent level (`duplicate_of`, every open answer identical), counted both ways, and never removed. Every table carries `department_id` and every query goes through a mandatory scope; row-level security is the escape hatch, not the first line.

## Alternatives considered

**A wide table, one column per question.** It matches the file. It also means a new schema per consultation, dynamic SQL for every filter, and `LIKE` over comma-joined strings for multi-select. Consult's public ADR 0006 chose the long shape and reports a benchmark on 500,000 respondents by 20 questions: a closed-question frequency in 68 ms and open answers filtered by three closed values in 218 ms (checked 18 September 2026). The benchmark is why I'm confident the shape holds at ten million rows, and docs/05-scale-and-cost.md says that I haven't reproduced it on the proposed instance class.

**No `attrs`; every demographic predicate as a semi-join on `answer`.** Correct, and fully normalised. Each predicate is another semi-join over a two-million-row table; `attrs` collapses all of them into one containment test on one index. It's derived and rebuilt, so the usual drift risk of a read model is bounded by the ingest job.

**Mutable themes and deletable tags.** The simplest CRUD, and it loses the answer to "what did the report say last Tuesday". A resumed job could also undo a person's edit. Rejected on provenance.

**A partial unique index on live tags only.** Would let a retracted tag be re-inserted as a fresh row. That's the resurrection case above; re-adding is an in-place update anyway, so the partial index buys nothing.

**pgvector from day one.** No dashboard filter needs it; the tsvector index covers search. Near-duplicate clustering is the named next step and would bring embeddings with it.

## Consequences

- Every number in the report is reproducible from a `theme_set_version_id` and a run id, and the manifest sheet lists both.
- Filters compose as ANDed predicates over one GIN index and two semi-joins, and the filter grammar in the URL maps onto them one to one.
- At an assumed twenty answer rows per respondent, 100,000 respondents is two million answer rows and up to two and a half million tags. RDS is sized from the first real consultation, not from this record.
- `attrs` duplicates data. A bug in the builder is a silent filter error, which is why a test will compare `attrs` with the answer rows on the fixtures after every ingest.
- The three-predicate plan is unbenchmarked on this schema. The acceptance test is `EXPLAIN (ANALYZE)` on a 20,000-row fixture showing the GIN and the `(question_id, id)` index in use, then the 500 ms SLO at ten million answers in the load test before go-live (ADR-007).

## How I'd know this was wrong

An `EXPLAIN` on the three-predicate filter that sequentially scans `respondent`, or a theme count on the dashboard that changes without a new `theme_set_version` row.
