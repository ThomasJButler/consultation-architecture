# Docs map

What each file in `docs/` (and the handful next to it) is for, and who it's
written for. Find the row that matches your question and start there.

| File | What it is for | Who reads it |
|---|---|---|
| `docs/README.md` | This map | Anyone opening `docs/` for the first time |
| `docs/00-brief-and-data-shape.md` | The task in my words: the requirement, the four-step user journey, what runs today, and the format of the responses file and the definition workbook | Anyone who wants the problem before the design; me, when writing the parsers |
| `docs/01-research.md` | Facts with a URL and a retrieval date, the public prior art, and the verification log | Anyone checking a number in the submission |
| `docs/02-architecture.md` | The master reference: assumptions, components, the pipeline, the consultation-level transition table, rollout, and the five screens as ASCII wireframes | Anyone asking why the design has this shape; the proof-of-concept is built against it |
| `docs/03-adrs/` | One record per decision, seven of them: context, decision, alternatives, consequences | Anyone who wants the alternative I turned down and what it would have cost |
| `docs/07-reviews.md` | One row per pull request: who reviewed it, what was found, what was fixed or kept | Anyone checking how the work was reviewed |
| `docs/diagrams/*.mmd` | Mermaid source for each of the two diagrams, written to a word budget | Whoever re-renders the diagrams |
| `docs/04-data-model.md` | Tables, keys and indexes, what each unique index makes idempotent, and the row-count maths | Me, writing `schema.sql`; anyone reading a query in the proof-of-concept |
| `docs/05-scale-and-cost.md` | Calls, tokens, cost and wall-clock at 1k, 10k and 100k responses, with the assumptions printed and the arithmetic shown | Anyone checking a cost or throughput figure |
| `docs/06-security-and-governance.md` | Controls tagged must, should and could; where the data goes and for how long; the governance artefacts a department would ask for | Anyone doing a DPIA, or asking what reaches the model |
| `THREAT_MODEL.md` | Assets, entry points, threats and the control that answers each, plus the logging policy | Anyone reviewing the design for security |
| `SECURITY.md` | How to report a problem, what's in scope, and how the brief is kept out of the tree | Anyone who finds a problem; anyone wondering where the sample data went |
| `SUBMISSION.md` | Source of the submitted document: both diagrams and the bullets | Everyone. It's the deliverable |
| `submission/index.html`, `submission/*.svg`, `submission/*.pdf` | The rendered submission: the page the PDF is printed from, the diagram renders and the PDF itself | Anyone who wants the document exactly as submitted |

## Conventions

- Once its pull request has merged, a document is amended under a
  `## Correction, <date>` heading and never edited silently.
- Every figure names its source and the date it was checked (`CLAUDE.md`, rule
  11). Anything that couldn't be verified is rounded or left out.
- The numbered files are in reading order.
- Files from later pull requests are added to this table as they land.
