# PR-07: Themes and sign-off

**Status:** Planned   **Owner:** Thomas Butler   **Date:** 25 September 2026
**Depends on:** PR-06   **Branch:** `feat/07-poc-themes-signoff`

## 0. What this PR does and doesn't do

**Locks in:** everything between a pending `find_themes` job and a
signed-off theme set, with the model a fake. The prompt contract from
`docs/06` section 2.2 (the stable prefix, the data preamble, the answers
as JSON-encoded data, the masks, and nothing from `attrs` or the vault);
the reply validator that holds model output to the schema, the enum and
the two-way id check in code (CLAUDE.md, rule 9); the `find_themes` job
driven stage by stage with a checkpoint per batch (docs/02, step 6;
ADR-002): generation over batches of about fifty distinct answers,
condensation to a capped shortlist with lineage, a preview over a
stratified sample that gives every candidate a count and quotes, then
`theme_set_version` v1 and fan-in 1 in one transaction; the reviewer's
edits guarded by the version's `edit_version`; and `consult` commands
that run a job with the fake, show a question's candidates as keys,
labels, counts and answer ids, and sign a question off. A stand-in
`jobs.queue` moves one job `pending` to `queued` so a command can claim
it.
**Doesn't yet cover:** dispatch under the caps, the worker loop and the
reconciler (PR-08); mapping and its retry at size one (PR-08, though the
validator it uses is built here); the filter query and exports (PR-09);
any real gateway call (ADR-005 puts one behind the `LLM` protocol, and the
proof-of-concept stops at the fake).

## 1. Objective

After `consult ingest` on the fixtures, `consult run-job <job id> --worker
w1 --model fake` takes the `o_reason` job from pending to succeeded: the
generation, condensation and preview stages each leave `job_batch` rows,
`theme_set_version` v1 exists as a candidate with a shortlist of themes
carrying `preview_count` and `theme_example` rows, and the question is
`themes_ready`. Running the second job flips the consultation to
`awaiting_review` and writes one `themes_ready` outbox row. `consult
themes <question id>` prints the candidates as keys, labels, counts and
example answer ids and never an answer. `consult sign-off <question id>
--reviewer <uuid> --expect-version 0` freezes v2 with `OTHER` and
`NO_REASON`, inserts the `map_themes` job pending, and a second sign-off
is refused. Every prompt the fake saw carried the preamble and the
answers as JSON, and none carried an `attrs` key or an email address.

## 2. Methodology

Four modules and two extensions. `consult/prompts.py` builds a `Prompt`
from a question, a theme list and a batch of answers: the prefix is the
role, the data line, the question text with the related closed answer
filled into its placeholder where the question has one, the theme keys
and the output schema; the user part is the answers as a JSON array of
`{"id", "text"}` after the masks (email, phone, UK postcode) have run on
the copy. `consult/replies.py` parses a `Completion` and returns typed
assignments or themes, raising `ReplyError` with
`ErrorCode.MODEL_OUTPUT_INVALID` for every fault the fakes can produce.
`consult/themes.py` is the `find_themes` job as stage functions over one
connection: `batches` (distinct answers by `text_sha256`, shuffled with a
seed stored in `job.params`, partitioned by the related closed answer,
cut at about fifty and a token cap), `generate`, `condense`, `preview`,
each checkpointing through `jobs.checkpoint` and each resumable from
`jobs.next_batch_no`, then `write_version` and `transitions.finish_find_themes`
in the caller's transaction. `consult/review.py` holds the edits (rename,
merge, split, add, remove) as guarded UPDATEs on `edit_version`, keys
stable throughout. The fakes gain a generation reply: `RecordingLLM`
answers a generation prompt with themes named from the batch, so the
mechanics can be proved without a model that reads. `jobs.queue` is the
one-line stand-in for dispatch, named as such in its docstring, and
PR-08 replaces its callers. Batches stay small enough that no transaction
approaches the ten-minute lease (`now()` is transaction start; PR-05's
security review, `docs/07` row 05). The alternative of calling
themefinder's top-level function was rejected in `docs/02` step 6 and
stays rejected: the checkpoints, the enum and the two-way check wrap the
stages, not the library.

## 3. Test plan (defined first)

1. `test_the_prompt_carries_the_contract_and_nothing_else` (pure) pins
   the prefix (role, data preamble, question text, theme keys, schema),
   the answers as JSON with their ids, the related closed answer filled
   into the placeholder, and the absence of `attrs` keys and of the vault
   value; a second case masks an email, a phone number and a postcode in
   the copy and leaves the stored text alone.
2. `test_a_reply_is_validated_in_code` (pure) pins the good reply parsing
   to assignments and every `Fault` in `tests/fakes.py` raising
   `ReplyError` with `MODEL_OUTPUT_INVALID`, including the duplicated id
   and the id nobody sent.
3. `test_generation_batches_distinct_answers_by_count_and_cap` pins
   batches over the fixture: duplicates once, the proforma included once,
   partitioned by `c_route` for `o_reason`, at most fifty per batch, the
   same order twice from the same seed.
4. `test_find_themes_checkpoints_every_batch_and_resumes` pins one
   `job_batch` row per stage per batch with `answer_ids`, and a second
   worker after a takeover starting at the next batch with the fake called
   only for the batches that hadn't finished.
5. `test_condense_caps_the_shortlist_and_keeps_lineage` pins at most
   seventy shortlist themes, the longlist kept with `is_longlist` and
   `lineage_theme_id` back to the candidates, and every key once per
   version.
6. `test_preview_gives_every_candidate_a_count_and_quotes` pins the sample
   (all answers when fewer than two hundred), `preview_count` on every
   theme, `theme_example` rows ranked and one per answer per theme.
7. `test_find_themes_writes_v1_and_flips_the_question_in_one_transaction`
   pins `theme_set_version` v1 as a candidate, the question `themes_ready`,
   the job succeeded, and, when it was the last open question, the
   consultation `awaiting_review` with one `themes_ready` outbox row.
8. `test_a_second_delivery_writes_no_second_v1` pins the unique index
   doing its job through the code path: a replayed job finds v1 and
   succeeds without a second version or a second theme row.
9. `test_edits_are_guarded_by_the_version` pins rename, merge, split, add
   and remove on a candidate version, each bumping `edit_version`, a stale
   expected version refused with nothing changed, and keys stable across
   a rename.
10. `test_the_run_job_command_runs_a_find_themes_job_with_the_fake` pins
    `consult run-job` end to end after `consult ingest`, its printed line
    holding counts and ids and no answer text.
11. `test_the_themes_command_prints_keys_labels_counts_and_ids` pins the
    listing and that no answer text reaches the terminal.
12. `test_the_sign_off_command_freezes_v2_and_refuses_a_second` pins
    `consult sign-off` through `transitions.sign_off`: v2 with the two
    fallbacks, the pending `map_themes` job, and exit 1 with nothing
    changed on the second attempt or a wrong `--expect-version`.

## 4. Implementation steps

Each ends in a commit; subjects are plain sentences; one failing test per
`Pin ...`.

1. `Pin the prompt contract` (test 1)
2. `Build the prompt from the contract and mask the copy` (`prompts.py`)
3. `Pin that a reply is validated in code` (test 2)
4. `Validate replies against the schema, the enum and the ids`
   (`replies.py`; the fakes gain the generation reply)
5. `Pin how generation batches the answers` (test 3)
6. `Batch distinct answers by count and cap with a stored seed`
7. `Pin the checkpoints and the resume` (test 4)
8. `Checkpoint every batch and resume from the last one`
9. `Pin the shortlist cap and the lineage` (test 5)
10. `Condense to a capped shortlist with lineage`
11. `Pin the preview counts and quotes` (test 6)
12. `Preview a stratified sample and write counts and quotes`
13. `Pin that v1 and the question flip land together` (test 7)
14. `Write v1 and finish the job in one transaction`
15. `Pin that a second delivery writes no second v1` (test 8)
16. `Make the replayed job find its version` (green if the index does it)
17. `Pin that edits are guarded by the version` (test 9)
18. `Guard every edit on edit_version` (`review.py`)
19. `Pin the run-job command` (test 10)
20. `Add run-job and the queue stand-in` (`cli.py`, `jobs.queue`)
21. `Pin the themes command` (test 11)
22. `Add the themes command`
23. `Pin the sign-off command` (test 12)
24. `Add the sign-off command`
25. `Say what the new test files prove` (`TESTING.md`, `poc/README.md`)
26. `Update the status block`

## 5. Output

| File | Purpose |
|---|---|
| `poc/consult/prompts.py` | The prompt contract and the masks |
| `poc/consult/replies.py` | Model output held to the schema, the enum and the ids |
| `poc/consult/themes.py` | The `find_themes` stages, checkpointed, then v1 and fan-in 1 |
| `poc/consult/review.py` | The reviewer's edits under the version guard |
| `poc/consult/jobs.py` | `queue`, the dispatch stand-in |
| `poc/consult/cli.py` | `consult run-job`, `consult themes`, `consult sign-off` |
| `poc/tests/fakes.py` | The generation reply |
| `poc/tests/test_prompts.py`, `test_replies.py`, `test_themes.py`, `test_review.py`, `test_cli_themes.py` | The tests above |

## 6. Security and quality notes

`THREAT_MODEL.md` rows 3 and 6 are the rows this pull request answers on
the prompt side: the containment test asserts what a prompt contains and
what it doesn't, and the validator turns every wrong-shaped reply into a
code, never a message body in `job.error`. The masks are a SHOULD in
`docs/06` and are tested as shapes, with the section 6 note that a name
in prose gets through. Every theme key the model returns is checked
against the version's enum before it is used as a label anywhere. A
reviewer should read `prompts.py` against `docs/06` section 2.2 line by
line, `replies.py` against `tests/fakes.py`'s faults, and the transaction
in `themes.write_version` against `docs/02` step 7.

## 7. Fallback

If driving stages with a fake makes the condensation step meaningless,
condense by key prefix in the fake's replies and say so in the test; the
mechanic under test is the cap, the lineage and the unique key, not the
grouping. If the preview's stratified sample is awkward without real
labels, sample by `c_route` partition and row number and say so.

## 8. Definition of done

- `make check` green locally and in CI.
- All twelve tests in section 3 exist and pass.
- `consult ingest`, `consult run-job` twice, `consult themes` and `consult
  sign-off` on the fixtures leave the schema as section 1 describes.
- `README.md` Status block updated; `plans/PR-08-*.md` written;
  `RESUME.md` updated.
