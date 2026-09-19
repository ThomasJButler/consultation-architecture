# PR-02: Data model, cost, security and governance

**Status:** Planned   **Owner:** Thomas Butler   **Date:** 19 September 2026
**Depends on:** PR-01   **Branch:** `docs/02-data-cost-security`

## 0. What this PR does and doesn't do

**Locks in:** the tables, keys and indexes the proof-of-concept will build; the
cost and throughput model with its assumptions printed; the security controls
and the governance story; a short threat model.
**Doesn't yet cover:** any code.

## 1. Objective

The proof-of-concept's `schema.sql` can be written straight from
`docs/04-data-model.md`, and every number in the submission traces to
`docs/05-scale-and-cost.md`.

## 2. Methodology

Derive the tables from the pipeline in `docs/02`; state each unique index and
what it makes idempotent. Print the cost assumptions and one line of arithmetic
per row of the cost table. Write the threat model as a short list of assets,
entry points, threats and the control that answers each (STRIDE-lite), not a
long document.

## 3. Test plan (defined first)

1. `sh scripts/brief-guard.sh --all` passes.
2. Every table in `docs/04` appears in the pipeline in `docs/02`, and every
   mechanism in `docs/02` names its table.
3. The cost table's figures follow from the printed assumptions (recomputed by
   hand once).

## 4. Implementation steps

1. `docs: write the data model with keys, indexes and what each makes idempotent`
2. `docs: write the scale and cost model with the arithmetic shown`
3. `docs: write the security and governance notes and the threat model`
4. `docs: update the status block`

## 5. Output

| File | Purpose |
|---|---|
| `docs/04-data-model.md` | Tables, keys, indexes, row-count maths |
| `docs/05-scale-and-cost.md` | Calls, tokens, cost, wall-clock at 1k, 10k, 100k |
| `docs/06-security-and-governance.md` | Controls tagged must, should, could; data handling; governance artefacts |
| `THREAT_MODEL.md` | Assets, entry points, threats, controls; logging policy |

## 6. Security and quality notes

The logging policy (ids, counts, durations, error codes; never answer text) is
written here and pinned by a test in PR-03.

## 7. Fallback

If the cost model cannot be reconciled with its assumptions, publish the range
and say which assumption drives the spread.

## 8. Definition of done

- The three checks in section 3 pass.
- `README.md` Status block and `RESUME.md` updated; `plans/PR-03-*.md` written.
