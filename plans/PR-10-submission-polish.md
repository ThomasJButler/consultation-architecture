# PR-10: The submission, finished

**Status:** Planned (drafted 26 September 2026)
**Owner:** Thomas Butler   **Date:** 26 September 2026
**Depends on:** PR-09   **Branch:** `docs/10-submission-polish`

## 0. What this PR does and doesn't do

**Locks in:** a repository that reads as finished.

- `SUBMISSION.md` checked against what the proof-of-concept proved, and the
  PDF rebuilt from it.
- The README's opening, Status and "How it was built" in their final form.
- `TESTING.md` naming a test for each of the four mechanics.
- `poc/README.md`'s "What it does not prove" cut down to what still isn't
  proved.
- The review log complete to row 10.
- An appended, dated correction wherever a design document says something
  the proof-of-concept found otherwise.

**Doesn't cover, by rule:** the wording of the submission bullets. The
owner rewrites them by hand (plans/00-plan.md, section 0). A session
proposes changes in the pull request description and never edits a bullet
itself. The tag `v1.0-submission` is the owner's, after the rewrite and the
merge, because nothing is rewritten after a tag (CLAUDE.md, rule 4).

## 1. Objective

A reader opening the repository cold finds:

- the submission;
- the two diagrams;
- the reasoning behind them;
- a proof-of-concept whose README and `TESTING.md` say what each test
  proves;
- a review log with a row per pull request.

None of it contradicts any other part. The PDF is rebuilt from the
owner's final bullets and fits three pages.

## 2. Methodology

Read, don't reinvent. Each bullet in `SUBMISSION.md` gets a row in the pull
request description: the claim, the document section or named test that
backs it, and whether the proof-of-concept now proves it, shows it
unproved, or contradicts it. The owner rewrites from that table. Anything
the proof-of-concept found wrong in `docs/` gets a dated correction
appended to that document, never an edit to the body above (docs/README's
rule). The rejected alternative, rewriting the bullets in a session, breaks
the plan's one hand-written deliverable.

## 3. Test plan (defined first)

This pull request is documents, so rule 2's red/green applies only where
a documentation rule can be checked by code:

1. `test_testing_md_names_a_test_for_each_mechanic` pins that the four
   mechanics docs/02 section 13 names each map, in `TESTING.md`, to a test
   that exists in `poc/tests/`.
2. `test_no_file_names_the_recruitment_process`, only if the owner decides
   the wording below should go. It pins rule 12 over the tracked files.

## 4. Implementation steps

1. `Pin that TESTING.md names a test for each mechanic` (test 1, red if any
   mechanic lacks a named test)
2. `Name a test for each of the four mechanics`
3. `Correct the design where the proof-of-concept found otherwise` (one
   commit per document, each a dated appended section)
4. `Cut what the proof-of-concept no longer leaves unproved` (`poc/README.md`)
5. `Finish the README's status and how it was built`
6. `Close the review log at row 10`
7. `Rebuild the PDF from the final bullets`, only after the owner's rewrite
   is on the branch. Until then the pull request says the PDF is pending.
8. `Update the status block to finished`

## 5. Output

| File | Purpose |
|---|---|
| `SUBMISSION.md`, `submission/*.pdf` | The owner's bullets, and the PDF built from them |
| `README.md`, `CLAUDE.md` | Final status and how it was built |
| `poc/README.md`, `poc/TESTING.md` | What is proved and by which test |
| `docs/*.md` | Appended corrections only |
| `docs/07-reviews.md` | Rows 08 to 10 |

## 6. Security and quality notes

- **The owner decides one thing.** README.md line 35 and `.gitignore`
  line 1 say the brief was "received under a recruitment process", which
  CLAUDE.md rule 12 forbids. A neutral wording keeps the handling point
  without the process, e.g. "The brief and the sample data for this task
  were shared in confidence and are marked OFFICIAL." The session puts this
  to the owner in the pull request and changes nothing without a yes.
- The brief guard runs as ever, and no figure goes into a document without
  a source and a date (rule 11).
- `submission/build.sh` pulls two pinned `npx` packages. If the
  environment can't run them (no Node, no headless Chromium), the PDF is
  the owner's to build locally, and the pull request says so.

## 7. Fallback

If time runs out before the owner's rewrite:

- the pull request carries the evidence table and every other change;
- the PDF from `v0.9-submission-draft` stands;
- the owner rewrites, rebuilds, merges and tags by hand.

## 8. Definition of done

- `make -C poc check` green (nothing under `poc/consult/` should change).
- Test 1 exists and passes.
- The pull request is open and ready for review, and its description holds
  the bullet-by-bullet evidence table and the rule-12 question.
- **Not merged by a session.** The owner rewrites the bullets, rebuilds the
  PDF, merges and tags `v1.0-submission`.
