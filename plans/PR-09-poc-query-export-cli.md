# PR-09: The filter query, the export and the plan benchmark

**Status:** Done (drafted 26 September 2026; merged as pull request #14 on
26 September 2026, with the open work its review left listed under
section 7)
**Owner:** Thomas Butler   **Date:** 26 September 2026
**Depends on:** PR-08   **Branch:** `feat/09-poc-query-export-cli`

## 0. What this PR does and doesn't do

**Locks in:** the fourth mechanic docs/02 section 13 names, and the export
the threat model's row 7 guards.

- **`consult/query.py`**, docs/04 section 6 as code:
  - the filter grammar from docs/02 step 11 (`attr:<column>=<value>`,
    `theme:<key>` OR'd on this question, `other:<question>.theme=<key>`,
    and `with=duplicates`), compiled to one `scope` CTE whose values are
    always parameters (docs/06 section 2, ADR-004);
  - the theme table with its denominator;
  - the related closed question's distribution.
- **`consult/export.py`**, docs/02 step 12's XLSX:
  - the original columns, one column per theme, a per-question summary sheet
    and a manifest sheet;
  - every cell written as a text cell;
  - a neutralising prefix on a value beginning with `=`, `+`, `-`, `@`, tab
    or carriage return, leaving a lone `-` alone (docs/06, the export row;
    THREAT_MODEL.md row 7);
  - rows read by the keyset query in docs/04 section 6, as the
    `consult_export` role.
- **Two commands:** `consult query` and `consult export`.
- **`scripts/make_fixture_data.py --scale N`**, a seeded 20,000-row
  consultation.
- **The plan benchmark:** load it, run ANALYZE, then walk the three-predicate
  filter's `EXPLAIN (ANALYZE, FORMAT JSON)` for a Bitmap Index Scan on
  `respondent_attrs_gin` and an Index Scan on `answer_question_id_id`
  (docs/04 section 6, docs/05 section 9).

**Doesn't yet cover:**

- The report as a print view or DOCX (docs/02 step 12's second kind), cut
  for time: the XLSX carries the same numbers.
- The overview endpoint, the response cards and their page query. Answer
  text on a screen belongs to the web app, and this command line prints no
  answer text.
- Presigned links and S3.
- The 500 ms at ten million rows, which stays with the staging load test
  (ADR-007).

## 1. Objective

On the fixtures, with tags from PR-08's worker or from a test factory,
`consult query <question> --filter attr:d_area=Villages --filter theme:<key>`
prints the theme table: keys, labels, counts and the denominator, and no
answer text. Its numbers match a count made by hand from `responses.csv`.
`consult export <consultation> --out out.xlsx` writes a workbook in which:

- every cell's type is text;
- the fixture's answer beginning with `=` reads back with its neutralising
  prefix;
- a lone `-` is still a lone `-`.

`pytest -m slow` loads 20,000 rows and finds both index scans in the plan.

## 2. Methodology

- **The query.** The filter is parsed into a small typed value first (pure,
  testable without a database). The builder then composes the CTE with
  `psycopg.sql`, so every user value is a placeholder and only fixed
  fragments are text. The builder takes only the parsed value, never a raw
  string, so there's no path from the grammar to SQL text.
- **The export.** `openpyxl`, already a dependency, writes in write-only
  mode. Every cell is set with its data type forced to string, and the
  prefix rule is one pure function with its own test. Identity columns
  follow docs/06 section 4: whatever it says the export may carry, and
  nothing more.
- **The benchmark.** The generator's distribution is chosen so `d_area =
  Villages` is selective enough for the planner to prefer the GIN index at
  20,000 rows, with the selectivity printed by the test, since that number
  is measured here (rule 11). The test's tags come from a factory: the
  benchmark measures the plan, not mapping.
- **The rejected alternative:** an ORM or a query-builder library. The
  design argues from the SQL in docs/04, and a builder would hide the
  statement a reviewer has to read.

## 3. Test plan (defined first)

1. `test_the_filter_grammar_parses_three_kinds_and_refuses_the_rest`
   (pure) pins each kind, the duplicate toggle, and a refusal by code for an
   unknown kind, an empty value or a malformed `other:`.
2. `test_hostile_filter_values_stay_parameters` hands the builder quotes,
   `%`, `;`, a comment marker, a NUL and a long value. It pins:
   - the composed statement's text identical whatever the values;
   - the rows correct (none);
   - the schema untouched.
3. `test_the_theme_table_counts_match_the_fixture` pins the counts and the
   denominator against a hand count from `responses.csv` with factory tags.
4. `test_the_other_filter_is_a_semi_join_across_questions` pins "tagged K
   on Q1" narrowing Q2's rows to those respondents.
5. `test_duplicates_are_hidden_unless_asked_for` pins both counts on the
   campaign proforma.
6. `test_export_values_are_neutralised_and_a_lone_dash_is_kept` (pure)
   pins each trigger character, a lone `-`, and a value with a trigger after
   the first character left alone.
7. `test_the_workbook_has_text_cells_every_sheet_and_the_manifest` pins:
   - the columns, one per theme, the summary and the manifest;
   - `data_type == 's'` on every cell read back.
8. `test_export_reads_as_the_export_role` pins the run under
   `consult_export`, and that it holds no write grant it would need.
9. `test_the_generator_scales_deterministically` pins the same bytes for
   the same seed and scale, and the row count.
10. `test_the_filter_plan_uses_both_indexes_at_twenty_thousand` (marked
    `db` and `slow`) pins the two plan nodes by walking the JSON, not by
    grepping text.
11. `test_the_query_and_export_commands` pins the objective through the
    CLI, and that no printed line carries answer text.

## 4. Implementation steps

Each is one commit through the tom-commit-voice skill. Each `Pin` commit
adds one failing test, run and seen failing for the right reason.

1. `Pin the filter grammar` (test 1)
2. `Parse the filter grammar into a typed value`
3. `Pin that hostile filter values stay parameters` (test 2)
4. `Compose the scope CTE with every value a placeholder`
5. `Pin the theme table against the fixture` (test 3)
6. `Count themes over the scope with the denominator stated`
7. `Pin the cross-question filter` (test 4)
8. `Add the other-question semi-join`
9. `Pin the duplicate toggle` (test 5)
10. `Hide duplicates unless the filter asks for them`
11. `Pin the neutralising prefix` (test 6)
12. `Neutralise formula triggers and keep a lone dash`
13. `Pin the workbook's sheets and text cells` (test 7)
14. `Write the export as text cells with a manifest`
15. `Pin that the export reads as its own role` (test 8)
16. `Run the export as the export role`
17. `Pin the scaled generator` (test 9)
18. `Scale the fixture generator with a seed`
19. `Pin the filter plan at twenty thousand rows` (test 10)
20. `Make the benchmark's filter selective enough to plan on the index`,
    or name what the plan needed
21. `Pin the query and export commands` (test 11)
22. `Add the query and export commands`
23. `Name the fourth mechanic's test in TESTING.md`
24. `Update the status block and write the plan for PR-10`, or revise the
    draft on `main`

## 5. Output

| File | Purpose |
|---|---|
| `poc/consult/query.py` | The grammar, the scope CTE, the theme table, the related distribution |
| `poc/consult/export.py` | The XLSX: text cells, the prefix, the summary and the manifest |
| `poc/consult/cli.py` | `consult query`, `consult export` |
| `poc/scripts/make_fixture_data.py` | `--scale N` |
| `poc/tests/test_query.py`, `test_export.py`, `test_plan_benchmark.py`, `test_cli_query.py` | The tests above |
| `poc/TESTING.md`, `poc/README.md` | The fourth mechanic proved, and what still isn't |

## 6. Security and quality notes

- THREAT_MODEL.md row 5: the parameterised filter and the department scope
  on every query.
- Row 7: formula injection, pinned by tests 6 and 7.
- The export role reads and never writes.
- No printed line or log line carries answer text. The export file does, by
  design, and the command says where it wrote it.
- A reviewer should read `query.py`'s composition against docs/04 section 6
  line by line, and the prefix function against docs/06's export row.

## 7. Fallback

- If the planner won't use the GIN index at any honest selectivity at
  20,000 rows, keep the test and assert what it does choose. Record the
  measured selectivity and the plan in `docs/05`'s unmeasured section as a
  dated correction, and say so in the pull request. A benchmark that finds
  the design wrong has done its job.
- If time runs short, cut in this order: the summary sheet, then test 5,
  then the related distribution. Never cut tests 2, 6 or 10.

**Open work the review round left (row 09), for a `fix/` branch:**

- `export.write_workbook` scopes by consultation id alone; the caller's
  department, which `query.scope` takes, isn't a parameter of the export
  yet (docs/06, section 2). The export reads the consultation's own
  department for its summary counts.
- The plan benchmark finds its two index nodes anywhere in the plan tree.
  The observed placement (the GIN scan feeding a Bitmap Heap Scan on
  `respondent`, the answer key probed on the inner side of a Nested Loop,
  200 loops) is recorded in `docs/05`'s correction of 26 September 2026
  and not yet asserted.

## 8. Definition of done

- `make check` green locally and in CI, and `pytest -m slow` green locally.
- All eleven tests exist, were each seen red first, and pass.
- The four mechanics are each proved by a named test, listed in
  `TESTING.md`.
- Row 09 is in `docs/07-reviews.md`, the README Status block is updated,
  and `plans/PR-10-*.md` is current.
