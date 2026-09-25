# Review log

One row per pull request. The review pass is ReviewBot Protocol running on my
own machine, with a Claude review pass before it. "Fixed" means a follow-up
commit on the same branch; "kept" means I disagreed and say why.

| PR | Branch | Reviewed by | Findings | Fixed | Kept, and why |
|---|---|---|---|---|---|
| 01 | `docs/01-design-freeze` | Claude review pass (10 findings); ReviewBot Protocol, `qwen3.5:9b` reviewed, `gemma4:12b` cross-examined (2 findings, 3 refuted by the cross-examiner) | Build fallback printed the wrong document and exited green; PDF had no author and a browser fingerprint; reopen path skipped the row lock; outbox weaker than ADR-006; "S3 archive only" contradicted the diagram; sign-off bullet read as a claim about another tool; dispatch waited on the five-minute schedule; docs map listed files that don't exist; diagram README had stale commands and type sizes; duplicates re-sent to the model | All ten, and both ReviewBot findings: the two `npx -y` calls in `build.sh` are now pinned to `@mermaid-js/mermaid-cli@11.17.0` and `md-to-pdf@5.2.5` | The three refuted `index.html` alt-text suggestions, on the cross-examiner's reasoning: stylistic, not WCAG |
| 02 | `docs/02-data-cost-security` | ReviewBot Protocol, same pair of models (no findings; six files skipped as prose); Claude review pass (15 findings, all reconciled in PR-02b) | SQS send restricted to the reconciler while dispatch happens in the web and worker; a milestone outbox rule with no SQL behind it that ordered random uuids; an unlogged staging table across a human step; erasure's external effects inside a transaction; no role holding DELETE on the vault; `department` dropped from a schema whose every table references it; an operator forbidden the vault lookup erasure needs; the 100k cost row on a different method from its neighbours; `run_id` minted nowhere; the duplicate toggle with no predicate; erasure deleting one trace where an answer sits in several; the `job.error` test deferred against three documents that say PR-03; two stale quotes of docs/02 in docs/05; a lifecycle rule reading a per-object date; four cut-list gaps | All fifteen, in PR-02b (#4) | |
| 02b | `docs/02b-reconcile` | ReviewBot Protocol, same pair, twice: eight files skipped as prose, then a markdown pass (14 findings, none upheld); Claude `/code-review high` (10 findings) | A null-subject attention row colliding with every later budget pause; reminders unable to fire again after a reopen; the reopen SQL sending a re-run to a state fan-in 1 never leaves; `consult_admin` too narrow for the retention job and named on the worker's path; the erasure retry re-reading a key step 7 had deleted; the deletion job's trace duty stated two ways; a minute of rounding; this log not yet carrying the pass six documents cite it for; the append-only corrections leaving superseded SQL in the bodies | Nine: the pause mints an id, reminders key on the candidate version, `subject_id` NOT NULL under a plain key, two reopen edges, `consult_admin` on every table with the worker off it, an idempotent erasure job, the deletion job deletes traces, 76 not 77, this row | The append-only correction sections: `docs/README.md`'s own rule for a merged document, and rewriting bodies would make the diff unreviewable; a pointer line under each heading now says the correction stands |

| 03 | `feat/03-poc-scaffold` | ReviewBot Protocol, `qwen3.5:9b` reviewed, `gemma4:12b` cross-examined (16 findings: 1 critical, 6 high, 5 medium, 4 low; 2 added by the cross-examiner, 33 refuted); a Claude review pass run as a six-lens workflow (schema, Python, security, tests, voice, fixtures) with three independent refuters per finding (32 findings, 17 upheld, 15 refuted) | ReviewBot: the CI service container's password as a hardcoded credential (twice); `consult init` as an undefined name in the status block; the password setting's comment against its code; `=1+1` in the fixture as an injection; the role guard as missing; three hook dependencies as unpinned; the proof-of-concept README claiming the mechanics; `Completion.text` unvalidated; an unused `UUID` import; a comment on token counts; `example.org` addresses. Claude pass: `consult_admin` granted UPDATE on `consultation` on the strength of superseded text; the formatter letting a mismatched `%s` call reach stderr with its arguments, `$` matching before a trailing newline, and `@` admitting an email address in an id-named field; `job.params` (jsonb) outside the allow-list the test pinned; a session database left behind when the schema fails to apply; the reset test passing when one table is dropped; init-twice pinned with no row to keep; the code vocabulary test one-directional; `TESTING.md` claiming every test was red once and not mapping the mechanics to their pull requests; the pre-commit comment putting the legacy hook after the configured ones; the errors comment citing `docs/02` for two cases it doesn't name; a 78-character commit subject; a "Make" commit red on its own test | Thirteen of the Claude pass's fifteen distinct findings (two were reported twice), in the ten commits after the review, and four of ReviewBot's: the role guard gained a handler for a concurrent first init, the status block points at `poc/README.md`, the password setting's purpose says blank counts as missing, and the proof-of-concept README's opening says what this pull request proves rather than what the finished thing will | The CI service password: it guards an ephemeral container reachable only from the job that already holds the value, and a Secret would hide from a reviewer a value that isn't one. The hook dependencies, pinned to the exact versions ReviewBot quoted. `UUID`, which is used. `=1+1` and the `example.org` addresses, which are the fixture's point. `Completion.text`, untrusted by design until PR-07's validator. The token comment, which says why the number is rough. The two findings on the branch's history (the 78-character subject and the red "Make" commit, both 9a5100f) need a rewrite of the branch, which the session couldn't do; the owner rewords before merging or accepts them |

## Markdown pass, 19 September 2026

ReviewBot's document reviewer went on after 01 and 02 had merged, so both
diffs were replayed as review-only pull requests (#5 and #6), reviewed with
markdown switched on, and closed unmerged. It raised 35 findings. Each was
checked against the text it read, against `main` and against 02b.

| Outcome | Findings |
|---|---|
| Already fixed on `main` | 4 |
| Trade-offs the ADRs state and accept | 2 |
| Kept: not a defect once the text is read | 29 |

Nothing changed as a result. The kept ones mostly came from reading each file
on its own, so every "see `docs/02`" read as a missing mechanism, and from
taking "Alternatives considered", "How I'd know this was wrong" and the rows
of this log as live claims. Two were stated backwards: Postgres NULL handling
in a unique constraint, and a pip-style `==` pin for `npx`. The document
reviewer is new, and these are the notes it gets next.

The markdown pass over this branch went the same way as the one over 01 and
02. Fourteen findings, four of them marked critical, none upheld. It read the
Correction sections as defect reports and filed the corrections back as the
defects, and it got two database facts backwards on pages this repository
already cites: Postgres does truncate an unlogged table after a crash, and
`NOT NULL` on the outbox subject is the reason the `NULLS NOT DISTINCT`
modifier goes, not a contradiction of it. Checking the two disputed facts did
turn up two small things of its own, fixed in the commit above.

