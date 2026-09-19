# Review log

One row per pull request. The review pass is ReviewBot Protocol running on my
own machine, with a Claude review pass before it. "Fixed" means a follow-up
commit on the same branch; "kept" means I disagreed and say why.

| PR | Branch | Reviewed by | Findings | Fixed | Kept, and why |
|---|---|---|---|---|---|
| 01 | `docs/01-design-freeze` | Claude review pass (10 findings); ReviewBot Protocol pending | Build fallback printed the wrong document and exited green; PDF had no author and a browser fingerprint; reopen path skipped the row lock; outbox weaker than ADR-006; "S3 archive only" contradicted the diagram; sign-off bullet read as a claim about another tool; dispatch waited on the five-minute schedule; docs map listed files that don't exist; diagram README had stale commands and type sizes; duplicates re-sent to the model | All ten | |
| 02 | `docs/02-data-cost-security` | pending | | | |
