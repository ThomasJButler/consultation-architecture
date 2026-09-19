# Review log

One row per pull request. The review pass is ReviewBot Protocol running on my
own machine, with a Claude review pass before it. "Fixed" means a follow-up
commit on the same branch; "kept" means I disagreed and say why.

| PR | Branch | Reviewed by | Findings | Fixed | Kept, and why |
|---|---|---|---|---|---|
| 01 | `docs/01-design-freeze` | Claude review pass (10 findings); ReviewBot Protocol, `qwen3.5:9b` reviewed, `gemma4:12b` cross-examined (2 findings, 3 refuted by the cross-examiner) | Build fallback printed the wrong document and exited green; PDF had no author and a browser fingerprint; reopen path skipped the row lock; outbox weaker than ADR-006; "S3 archive only" contradicted the diagram; sign-off bullet read as a claim about another tool; dispatch waited on the five-minute schedule; docs map listed files that don't exist; diagram README had stale commands and type sizes; duplicates re-sent to the model | All ten, and both ReviewBot findings: the two `npx -y` calls in `build.sh` are now pinned to `@mermaid-js/mermaid-cli@11.17.0` and `md-to-pdf@5.2.5` | The three refuted `index.html` alt-text suggestions, on the cross-examiner's reasoning: stylistic, not WCAG |
| 02 | `docs/02-data-cost-security` | ReviewBot Protocol, same pair of models (no findings; six files skipped as prose); Claude review pass pending | | | |
