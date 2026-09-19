# PR-01: Design freeze

**Status:** Planned   **Owner:** Thomas Butler   **Date:** 19 September 2026
**Depends on:** PR-00   **Branch:** `docs/01-design-freeze`

## 0. What this PR does and doesn't do

**Locks in:** the architecture, the decision records, both diagrams, a first
draft of the submission and a first rendered PDF. After this PR the design does
not change without a correction note in the affected document.
**Doesn't yet cover:** the data model in detail, the cost model, security and
governance (PR-02), and the final wording of the submission (PR-10).

## 1. Objective

A submittable document exists at the end of this PR, even if nothing after it
lands. The design is frozen so that the proof-of-concept builds against a fixed
target.

## 2. Methodology

Write the brief in my own words from memory of its requirements, not from its
text. Write the research as a list of sourced facts with retrieval dates; any
figure without a source is rounded or left out. Write the architecture as the
master reference and derive the decision records from it (six to eight, each
with alternatives and consequences). Draw the diagrams last, to a word budget,
and render them to SVG. The alternative, writing the submission first and the
reasoning after, was rejected because the diagrams are only defensible once
the reasoning is written down.

## 3. Test plan (defined first)

Docs have no unit tests. The checks are:

1. `sh scripts/brief-guard.sh --all` passes.
2. Both diagrams render with `npx -y @mermaid-js/mermaid-cli` and each meets
   its budget: nine nodes at most, eight words per node, five words per edge
   label, ninety words in total, a caption of twenty-five words.
3. The PDF opens, is under 10 MB, is two or three pages, and its diagram text
   is at least 8 pt when printed at A4.
4. Every number in `docs/01-research.md` has a URL and a retrieval date.

## 4. Implementation steps

1. `docs: describe the brief and the data shape in my own words`
2. `docs: record the research with sources and retrieval dates`
3. `docs: write the architecture reference`
4. `docs: add the decision records`
5. `docs: draw both diagrams to the word budget and render them`
6. `docs: draft the submission and build the first PDF`
7. `docs: add the docs map and update the status block`

## 5. Output

| File | Purpose |
|---|---|
| `docs/README.md` | Map: file, what it is for, who reads it |
| `docs/00-brief-and-data-shape.md` | Requirements and the definition-workbook format, paraphrased |
| `docs/01-research.md` | Sourced facts, prior art, verification log |
| `docs/02-architecture.md` | Master reference: assumptions, components, pipeline, rollout, transition table, wireframes in ASCII |
| `docs/03-adrs/ADR-00N-*.md` | Six to eight decision records |
| `docs/diagrams/*.mmd`, `submission/*.svg` | Diagram sources and renders |
| `SUBMISSION.md`, `submission/index.html`, `submission/*.pdf` | Draft submission and the first PDF |

## 6. Security and quality notes

No brief text, no pilot claims, no authorship, no internal hostnames, no claims
about what the incumbent team has or has not built. Prior art is cited as
public documents. The PDF's metadata carries my name and title only.

## 7. Fallback

If Mermaid will not fit the budget at A4, draw the diagram by hand in
Excalidraw and export SVG. If the Chromium print path fails, `npx md-to-pdf`.

## 8. Definition of done

- All four checks in section 3 pass.
- A PDF that could be submitted as-is exists in `submission/`.
- `README.md` Status block and `RESUME.md` updated; `plans/PR-02-*.md` written.
