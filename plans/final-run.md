# The final run: PR-08, PR-09 and PR-10

**Status:** Run on 26 September 2026: PR-08 (#13) and PR-09 (#14) merged, PR-10 (#15) open for the owner   **Owner:** Thomas Butler   **Written:** 26 September 2026

One file a fresh session can run from a clone, with nobody at the keyboard.
It carries what `RESUME.md` and `plans/prompts/` would, because both are
gitignored and a clone won't have them. The deadline is **Monday 28
September 2026**, and the owner is at work that day, so everything below
is finished, or honestly stopped, by **Sunday 27 September 23:00 UK
time**.

## 0. How to start it

Start the cloud session on **Fable 5.1** (`claude-fable-5-1`), then paste
this:

> Run `plans/final-run.md` in this repository from section 1 to the end.
> Merging is delegated to you for PR-08 and PR-09: merge each with a
> merge commit once its checks are green and its review round is fixed and
> logged. PR-10 is opened and left for me. Use the `tom-commit-voice` skill
> for every commit, pull request and merge message, and the
> `mattpocock-skills:tdd` skill for the test loop, where they're
> available. Sections 4 and 5 of the file are the rules either way.
> Orchestrate on Fable 5.1 and hand the work to Sonnet 5 and Opus 5.5
> subagents as section 3 sets out, within its budget. You may run a
> workflow for each review round, inside the same budget.

That paragraph is the owner's delegation. It overrides CLAUDE.md rule 5's
"the owner does the merging" for PR-08 and PR-09 only, as the owner has
done for every merge since PR-04. Without it, stop at each ready pull request.

## 1. The order and the clock

| # | Pull request | Branch | Session estimate | Merged by |
|---|---|---|---|---|
| 1 | PR-08 mapping, worker, reconciler | `feat/08-poc-mapping-worker` (exists) | 2 to 3 h | the session |
| 2 | PR-09 filter query, export, benchmark | `feat/09-poc-query-export-cli` | 1.5 to 2 h | the session |
| 3 | PR-10 submission polish | `docs/10-submission-polish` | 45 to 75 min | the owner |

The estimates are measured from PR-03 to PR-07's history. From one merge
to the last commit of the next branch, each took 50 minutes to 1 h 50,
with a full review round inside it.

Read the time as `TZ=Europe/London date` at the start of each pull request
and apply the first rule that matches:

- **PR-09 starting after Sunday 16:00:** cut to its tests 1, 2, 6, 9 and 10
  and `consult query`, and say so in the pull request.
- **PR-10 starting after Sunday 20:00:** do its steps 1 and 2 and the
  evidence table only.
- **Review round:** 45 minutes at most. If the clock rules are tight, run
  the security review and one code-review pass, and log that in the row.
- **Sunday 23:00 is the hard stop.** Push whatever is in flight, open or
  update its pull request as ready with an honest description of what's
  done and what isn't, and stop.

## 2. The environment

The environment must be green before any work starts. The owner's laptop is
closed for the whole run: nothing here may depend on it, its Docker
Postgres, `RESUME.md` or `plans/prompts/`. The owner tests by hand
afterwards (section 10).

1. **Identity.** A clone has none of the repo-local config:

   ```bash
   git config user.name "Thomas Butler"
   git config user.email dev@thomasjbutler.me
   ```

2. **Python 3.12 or later.**
   - Run `make -C poc venv` if `python3.12` exists.
   - Otherwise:

     ```bash
     python3 -m venv poc/.venv
     poc/.venv/bin/pip install -e 'poc[dev]'
     ```

   - Then install the hook: `poc/.venv/bin/pre-commit install`.

3. **Settings.** Run `cp poc/.env.example poc/.env` and change
   `CONSULT_DB_PASSWORD` from `change-me` to any value.

4. **Postgres 17 on 127.0.0.1:5432**, with the role, password and database
   `poc/.env` names. Take the first that works:
   - Docker: `cd poc && docker compose up -d db`.
   - apt, through the PGDG repository:

     ```bash
     sudo apt-get install -y postgresql-common
     sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
     sudo apt-get install -y postgresql-17
     ```

     Then start the cluster and create the login role (with `CREATEROLE`,
     since `consult init` creates the four grant roles) and the database.
   - Only 16 installable: use it, and say so in the pull request. CI's
     Postgres 17 is then the gate.
   - None of these: open the pull request as a draft at its first commit
     and use CI as the database. Every push runs the suite in about three
     minutes. Mark it ready at the end.

5. **Green before starting.** `make -C poc init`, then `make -C poc check`,
   must be green before the first test is written. If `main` isn't green,
   that's the first thing to fix and report.

6. **GitHub.** Use `gh` if it's there. Otherwise the GitHub MCP tools
   (create, review, merge a pull request) if they are. If neither is
   available, push the branch and stop, since the chain can't continue
   without a merge.

## 3. Models and the budget

**The session runs on Fable 5.1 as the orchestrator.** Fable reads the
plans, sequences the work, and checks every chunk that comes back. It also
decides fallbacks and judges disputed review findings. It writes every word
that goes out under the owner's name: the pull request descriptions, the
merge messages, the review rows and the reports.

**Everything else goes to subagents, and they are Sonnet 5 or Opus 5.5,
never Fable.**
- Every Agent call and every Workflow `agent()` call names its model:
  `model: "sonnet"` for Sonnet 5, `model: "opus"` for Opus 5.5.
- A call that leaves the model out inherits Fable, which is the one thing
  this budget exists to stop.
- In a workflow, set `effort: "medium"` on Sonnet agents and
  `effort: "high"` on Opus agents.

### Who does what

| Work | Model | Why that one |
|---|---|---|
| Sequencing, fallbacks, judging disputed findings, every description, merge message, review row and report | Fable 5.1, the session itself | Judgement over the whole run's context |
| Checking a chunk: run its tests, read the diff, check the subjects and trailers | Fable 5.1, inline, no subagent | A few shell commands don't need an agent |
| Getting the environment green (section 2) | Sonnet 5 | Long, noisy, well specified |
| Implementation chunks that are concurrency, locking or SQL composition | Opus 5.5 | Where a subtle bug costs the most, and where the review rounds found the real defects (`docs/07` rows 05 and 06) |
| Implementation chunks that are pure functions, wiring, settings, fakes, docs or status | Sonnet 5 | Well specified by the plan and pinned by a test |
| Review lenses: design fidelity, tests and red/green history, writing rules | Sonnet 5 | Reading against a checklist |
| Review lenses: SQL and Python under concurrency; the security review | Opus 5.5 | The hard reading |
| Verifying findings | One Sonnet 5 agent per lens verifies all of that lens's findings in one pass. Only findings it upholds at high severity get one Opus 5.5 refuter | Three refuters per finding on forty findings would be 120 agents |
| Fixing upheld findings | Sonnet 5, or Opus 5.5 when the fix is in a concurrency path | |

### The chunks

Implementation runs **one chunk at a time** because it's one branch. Each
chunk agent does its red/green pairs in order, commits each one to sections
4 and 5, and reports back. Its report gives:
- the hashes and subjects of its commits;
- the failing line it saw for each `Pin`;
- anything the plan didn't answer.

The agent's brief names the plan file, its step numbers, the files it may
touch, and sections 4 and 5 of this file.

| PR | Chunk | Plan steps | Model |
|---|---|---|---|
| 08 | The pure pick | 1-2 | Sonnet 5 |
| 08 | Dispatch under the lock; `jobs.queue` retired | 3-4 | Opus 5.5 |
| 08 | `start_map_themes` and `fail_job` | 5-6 | Opus 5.5 |
| 08 | Mapping batches and duplicates | 7-10 | Sonnet 5 |
| 08 | The retry at one, resume by coverage, fan-in 2 | 11-16 | Opus 5.5 |
| 08 | `GatewayError` and the backoff | 17-18 | Sonnet 5 |
| 08 | The worker loop | 19-20 | Opus 5.5 |
| 08 | The reconciler's statements | 21-28 | Opus 5.5 |
| 08 | The commands, the docs correction, docstrings, `TESTING.md` | 29-33 | Sonnet 5 |
| 09 | The grammar | 1-2 | Sonnet 5 |
| 09 | The scope CTE and hostile values | 3-4 | Opus 5.5 |
| 09 | The theme table, the other-question filter, the duplicate toggle | 5-10 | Sonnet 5 |
| 09 | The prefix, the workbook, the export role | 11-16 | Sonnet 5 |
| 09 | The generator and the plan benchmark | 17-20 | Opus 5.5 |
| 09 | The commands and `TESTING.md` | 21-23 | Sonnet 5 |
| 10 | The mechanics pin and `TESTING.md` | 1-2 | Sonnet 5 |
| 10 | The docs corrections and the READMEs | 3-6 | Sonnet 5 |

Fable does the status-block step at the end of each PR itself, because it
carries the owner's words. Fable writes PR-10's evidence table from what a
Sonnet agent pulls out of `SUBMISSION.md` and the docs.

### The budget

| PR | Subagents at most | Of which Opus 5.5 at most |
|---|---|---|
| 08 | 24 | 10 |
| 09 | 18 | 6 |
| 10 | 6 | 1 |
| **The run** | **48** | **17** |

- No more than five subagents run at once, and only in a review round.
- Fable subagents: none.
- Fable keeps a running count in its head and writes it into each PR's
  description and review row. The row names which model ran which lens.
- When a PR's ceiling is reached, no more subagents start on it. Fable
  finishes inline, or cuts to the clock rules in section 1, and says so.
- If the account's usage limit is hit mid-run, push what's committed and
  stop as section 11 says.

## 4. The test loop (binding)

This follows CLAUDE.md rule 2 and the `mattpocock-skills:tdd` skill. If
the skill is listed, invoke it at the start of each pull request. These
rules hold either way:

1. **One behaviour at a time, in the plan's order.** Write the one test the
   next `Pin ...` step names, and nothing else.
2. **Run that test alone and watch it fail**
   (`poc/.venv/bin/pytest poc/tests/test_x.py::test_name -x`).
   - The failure has to be the right one: the assertion, or the missing
     name the green commit will create.
   - A test that passes on arrival pins nothing new. Say so in the commit
     body, or change the test until it fails for the right reason.
3. **Commit the red:** `Pin ...`. Pre-commit runs ruff and mypy on
   `poc/consult/` only, so a red test commits.
4. **Write the least code that turns it green.** Run the test, then
   `make -C poc lint type` and the file's neighbours. Commit the green,
   named for the change.
5. **The green commit doesn't touch the test.** If the test was wrong,
   that's its own commit, and its body says why.
6. **Expected values come from outside the code under test:** the design
   (a docs section), the fixtures' CSV, the fakes' script, or a hand count
   written in the test.
7. **Refactor only on green**, in its own commit.
8. **`make -C poc check` in full before every push.** Coverage stays at 90%
   or above.

## 5. The commit voice (binding)

This follows CLAUDE.md rule 4 and the `tom-commit-voice` skill. If the
skill is listed, invoke it for every commit, pull request description and
merge message. These rules hold either way:

**Subjects**
- A plain imperative sentence: capital first letter, 72 characters at
  most, no full stop, no `feat:` or other type prefix.
- A red test reads `Pin ...`. The green one reads `Make ...` or names the
  change.

**Bodies**
- Every commit in this run gets a body.
- It says why, or what the diff can't: the design section it follows, the
  fact that forced the shape, the number measured.
- Wrapped at about 72 characters.

**Voice**
- British English. Contractions welcome.
- Short sentences. Dry is fine, and cheer isn't needed.
- The problem is described, never a person.

**Never**
- An em dash (U+2014).
- `This commit`, `This PR`, `leverage`, `utilise`, `robust`, `seamless`,
  `streamline`, `delve`, `Furthermore`, `Moreover`, `Additionally`,
  `genuinely`, `truly`, `simply`, `essentially`.
- **No `Co-Authored-By`, no `Claude-Session` trailer, no session link, no
  `Generated with` line.** Whatever the tool appends, check
  `git log -1 --format=%B` after every commit and amend it out before
  pushing.

**Pull request description:** a title that's a plain sentence, then
`## What`, `## Why`, `## How` (bullets for the decisions a reviewer
wouldn't guess), `## Testing` (the test count and what ran where), and
`## Notes` (where to look first). PR #11 is the model.

**Merge:** `gh pr merge <n> --merge`.
- The subject is `Merge pull request #<n> from ThomasJButler/<branch>`.
- The body is the pull request's title, a blank line, then two or three
  sentences on what now holds, with the commit count and how many of them
  were the review round.
- `git log -1 --format=%B fb8bb1e` is the model.

## 6. Every pull request finishes the same way

1. `make -C poc check` green. Push.
2. Open the pull request **ready for review**, not draft (unless section 2
   made it a draft; mark it ready now). Wait for CI green.
3. **The review round**, 45 minutes at most:
   - **Code review:** the `code-review` skill at `high`, or, if the
     Workflow tool is available, five lenses with three refuters per
     finding. The lenses:
     - design fidelity against the docs the plan cites;
     - Python and SQL correctness under concurrency;
     - the threat model's guards and the logging policy;
     - the tests and the red/green history;
     - the writing rules.
   - **Security review:** the `security-review` skill.
   - Fix upheld findings as red/green pairs on the branch.
4. **Log row `NN` in `docs/07-reviews.md`, with the numbers:** found,
   upheld, refuted, fixed, kept and why. Count as you go. PR-07's counts
   were lost because they weren't written down before the merge.
5. Update the README Status block (dated) and CLAUDE.md's status line.
   Revise the next plan if this merge changed anything it assumes.
6. For PR-08 and PR-09: CI green on the head, merge (section 5), then run
   `git switch main && git pull`. For PR-10: stop here.
7. Report in fifteen lines at most: what shipped, what was cut and why,
   and anything the owner must decide.

## 7. PR-08: mapping, the worker loop and the reconciler

**Plan:** `plans/PR-08-poc-mapping-worker.md`. It was revised on
26 September against the merged PR-07 code. Its section 2 is the list of
what the first version got wrong; read it before the first test.

**Pre-flight:** `main` contains PR-07 (`poc/consult/themes.py` exists).
Run `git switch feat/08-poc-mapping-worker`. It's on the remote with the
planning commits at its base; don't cut a new one.

**Scope in:**
- dispatch under the three caps, as a pure pick plus one UPDATE under an
  advisory lock;
- `start_map_themes` and `fail_job`;
- the `map_themes` job: batches of ten shuffled with the stored seed, the
  two-way check, the retry at size one and the `unprocessable` bucket,
  tags copied to duplicates, and resume by coverage;
- `GatewayError` and the backoff, with no transaction open;
- the worker loop;
- the reconciler's statements, with the fifth-attempt fix and its docs/02
  correction;
- `consult worker [--once]` and `consult reconcile`;
- `jobs.queue` retired.

**Scope out:**
- the `stage` and `ingest` job rows, and `review_reminder` rows (deferred,
  with the plan's reasons);
- the filter query and exports;
- any real gateway, SQS or Notify call.

**Watch for:**
- Every worker write goes through the fence.
- Run the job as the pipeline role (`as_role`), as `run-job` does.
- Use `raise … from None` wherever a reply could ride an exception chain.
- No answer text in a log line, a printed line or an exception message.
- Log field names pass `consult/logs.py`'s allow-list.
- The jitter's `random` carries both the ruff S311 and the bandit B311
  markers.

## 8. PR-09: the filter query, the export and the plan benchmark

**Plan:** `plans/PR-09-poc-query-export-cli.md`.

**Pre-flight:** `main` contains PR-08 (`poc/consult/worker.py` exists).
Run `git switch -c feat/09-poc-query-export-cli main`.

**Scope in:**
- the filter grammar parsed to a typed value;
- the scope CTE composed with `psycopg.sql`, every value a placeholder;
- the theme table and the related distribution;
- the XLSX export: text cells, the neutralising prefix, the manifest, read
  as `consult_export`;
- `consult query` and `consult export`;
- `make_fixture_data.py --scale`;
- the EXPLAIN benchmark at 20,000 rows, marked `slow`;
- the fourth mechanic named in `TESTING.md`.

**Scope out:** the DOCX or print report, the overview, the response cards,
presigned links, and ten million rows.

**Never cut:** tests 2, 6 and 10 (hostile values, the prefix, the plan).
If the planner won't use the GIN index at an honest selectivity, the
plan's section 7 says what to record. A benchmark that finds the design
wrong has done its job.

## 9. PR-10: the submission, finished

**Plan:** `plans/PR-10-submission-polish.md`.

**Pre-flight:** `main` contains PR-09 (`poc/consult/query.py` exists).
Run `git switch -c docs/10-submission-polish main`.

**Scope in:**
- the evidence table for every `SUBMISSION.md` bullet, **in the pull
  request description, not the file**;
- the four mechanics named in `TESTING.md`, pinned red first;
- appended, dated corrections wherever the proof-of-concept found a design
  document wrong;
- `poc/README.md`'s unproved list;
- the README's final status and how it was built;
- rows 08 to 10 complete.

**Never:**
- edit a submission bullet (the owner writes those by hand);
- tag;
- merge;
- change the rule-12 wording in README.md line 40 and `.gitignore` line 1
  without the owner's yes. Ask in the description, with the neutral
  wording the plan suggests.

**Finish:** CI green, ready for review, then stop. The report ends with
the owner's list: rewrite the bullets, rebuild the PDF
(`submission/build.sh`), answer the rule-12 question, merge, and tag
`v1.0-submission`.

## 10. The owner's manual test

The owner runs this on the laptop after the run, on the way home, in about
half an hour. It's not a gate on the run. Anything it finds goes on a
`fix/` branch cut from `main` as a red/green pair, before PR-10 merges.

```bash
cd ~/Repos/consultation-architecture && git switch main && git pull
cd poc && docker compose up -d db
.venv/bin/pip install -e '.[dev]'
make reset && make check
make ingest                                # prints the consultation id
docker compose exec db psql -U consult -d consult -c \
  "select id, column_ref, status from question where kind = 'open'"
.venv/bin/consult worker --once            # run twice: both questions themes_ready
.venv/bin/consult worker --once
.venv/bin/consult themes <question-id>     # read the themes and the edit counter
.venv/bin/consult sign-off <question-id> --reviewer "$(uuidgen)" --expect-version <n>
                                           # once per question
.venv/bin/consult worker --once            # run twice: both complete, consultation ready
.venv/bin/consult worker --once
docker compose exec db psql -U consult -d consult -c \
  "select status from consultation; select kind, status from notification_outbox;
   select kind, status, count(*) from job group by 1, 2"
.venv/bin/consult reconcile                # then the same query: only the outbox rows move to sent
```

After PR-09 (the flags as `consult query --help` shows them):

```bash
.venv/bin/consult query <question-id> --filter attr:d_area=Villages
.venv/bin/consult export <consultation-id> --out ~/Desktop/consult.xlsx
open ~/Desktop/consult.xlsx                # the answer starting "=" shows as text, not a formula
.venv/bin/pytest -m slow                   # the plan benchmark
```

What to look for:
- No answer text anywhere in the terminal. Ids, counts, codes and
  durations only.
- The consultation goes `processing`, then `awaiting_review`, then `ready`.
- One `themes_ready` row and one `analysis_ready` row.
- `reconcile` on a finished consultation changes no job row.
- In the workbook, a lone `-` is still a lone `-`.

## 11. When to stop early

Stop, push, and report instead of guessing, if:
- CI goes red for a reason that isn't fixed in 30 minutes;
- a plan's fallback would weaken one of the four mechanics;
- a merge conflict lands outside `cli.py`, the README, CLAUDE.md or
  `plans/`;
- anything asks for a file under `~/iai-brief-private/`.

Everything else a plan doesn't answer: take its section 7 fallback, and
say so in the commit body and the pull request.

## 12. State at hand-off

On 26 September 2026:
- `main` has PR-03 to PR-07 (#7 to #11) and Dependabot's setup-python
  7.0.0 (#12). `poc/tests/` held 141 test functions at PR-07's merge.
- `feat/08-poc-mapping-worker` carries this run's planning commits:
  - PR-07's review row, rebuilt from history;
  - the status lines;
  - the revised PR-08 plan;
  - the drafted PR-09 and PR-10 plans;
  - this file.

  Its first code commit is PR-08's step 1.
