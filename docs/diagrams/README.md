# Diagrams

Two Mermaid sources. The submission renders them to SVG; nothing else in the
repository depends on them. The budget is the definition of done in
`plans/PR-01-design-freeze.md`, section 3: nine nodes at most, eight words in a
node label, five in an edge label, ninety words in a diagram, and a one-sentence
caption of twenty-five words or fewer. There's no observability node in either;
that's a bullet in the submission.

## Diagram 1: the journey and the system

Source: `diagram-1-journey-and-system.mmd`.

Caption: One database holds every fact and job, reviewers sign off before
tagging, and each email is written in the commit that earns it.

The four numbered steps across the top are the user's journey, and each points
at the box that serves it. Nine nodes won't hold every container, so I chose
what to fold. The web app is drawn twice, before any model spend and after
processing. That split is what keeps the boxes under the steps they serve, in
order: dagre, Mermaid's layout engine, orders a row by where its edges go, and
with one web box under four steps it put step 3 wherever it liked (tried here,
19 September 2026). The reconciler shares the worker's box, since it's the same
image on a different schedule. S3 and SQS are edge labels. Neither holds state
the pipeline depends on: the upload is read once by a job, queue messages are
hints and the job table is authoritative (`docs/02-architecture.md`,
section 3). The LLM gateway is named in the worker's box. The reviewer's
sign-off is the labelled path from step 3 to the sign-off screen, which sits
between step 3 and step 4; Diagram 2 draws the same sign-off as a hard state.
The spacing directive at the top tightens the layout so it prints larger and
changes nothing else.

## Diagram 2: one open question's life

Source: `diagram-2-question-lifecycle.mmd`.

Caption: Each question runs, fails and retries alone; awaiting_review, ready
and both emails come from one guarded UPDATE in the commit finishing a
question.

The chain is one open question's states. The two identical edge labels are the
fan-in. The transaction that moves a question into `themes_ready` or `complete`
also checks its siblings, and if this is the last one it flips the consultation
and writes the outbox row before it commits (`docs/02-architecture.md`,
section 4). None of that lives in a note. `find_failed` and `map_failed` are
reached after five attempts, each resumed from the last checkpoint, and an
operator retry sends the question back round. The consultation-level state
names wouldn't fit in five words next to "one transaction", so the caption
carries them.

## Word budget

| | Nodes | Longest node label | Longest edge label | Words | Caption |
|---|---|---|---|---|---|
| Budget | 9 | 8 | 5 | 90 | 25 |
| Diagram 1 | 9 | 8 | 5 | 78 | 23 |
| Diagram 2 | 8 | 1 | 5 | 42 | 23 |

Words are whitespace-separated tokens in node labels and edge labels. Mermaid
keywords, node ids, comments and the init directive aren't counted. A caption
is counted without the "Diagram N." prefix the submission prints in front of
it; with the prefix each is 25, and the submission carries these captions
word for word. Counted on 19 September 2026 against the files as committed.

## Rendering

```sh
bash submission/build.sh   # renders both SVGs (white background) and prints the PDF
```

Measured with mermaid-cli 11.17.0 on 19 September 2026: Diagram 1 renders
935 px wide and Diagram 2 644 px, at Mermaid's default 16 px type. On the landscape
sheets `index.html` prints them on, capped at 138 mm and 158 mm high, the
16 px labels come out at about 9.8 pt and 10.3 pt. The floor is 8 pt
(`plans/PR-01-design-freeze.md`, section 3), so both clear it, without much
to spare on Diagram 1.
