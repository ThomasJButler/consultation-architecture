You are continuing work in the consultation-architecture repository.

Context: a design for a production consultation-analysis service and a small
proof-of-concept of its job-pipeline mechanics on Postgres. The deliverable is a
two-page document; everything else is working material behind it.

Pre-flight:
1. Read `RESUME.md`, `CLAUDE.md` and `plans/PR-NN-<slug>.md`.
2. Confirm `main` is at or after tag `<tag>` and that PR-<nn-1> is merged. If
   not, stop and say so.
3. `git switch -c <type>/<nn>-<slug> main`

Scope in: ...
Scope out: ...

Constraints: follow every numbered rule in `CLAUDE.md`. Tests first. Commit
subjects are plain imperative sentences with no type prefix, through the
tom-commit-voice skill. No new dependency without a pinned
version and a one-line reason. Never read or reference `~/iai-brief-private/`.

The work: the ordered tests and commits in section 4 of the plan file.

Finish: `make check` green (where it exists), open the pull request as a draft
with a description through the skill, run the reviews the merge policy asks
for, fix findings on the same branch, merge with `--no-ff` if the policy allows,
update the README Status block, write the plan and prompt for the next PR,
update `RESUME.md`, then report in fifteen lines at most: what shipped, what is
deferred, anything the owner must decide.
