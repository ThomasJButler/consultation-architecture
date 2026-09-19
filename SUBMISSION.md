# Consultation analysis: a production architecture

> Draft v0. The owner rewrites these bullets by hand before submission.

Thomas Butler

One central service that turns a department's consultation responses into signed-off themes and a filterable dashboard.

## Diagram 1: the journey and the system

![Diagram 1: the journey and the system](submission/diagram-1-journey-and-system.svg)

*Diagram 1. One database holds every fact and job, reviewers sign off before tagging, and each email is written in the commit that earns it.*

## Diagram 2: one open question's life

![Diagram 2: one open question's life](submission/diagram-2-question-lifecycle.svg)

*Diagram 2. Each question runs, fails and retries alone; awaiting_review, ready and both emails come from one guarded UPDATE in the commit finishing a question.*

## The design, in bullets

- A named reviewer signs off each question's themes; every edit is kept. Uploads are validated and costed before spend; no demographic reaches a prompt. One platform serves every department.
- The journey gains one step: a reviewer signs off each question's themes before tagging, server-enforced. In i.AI's published evaluation reviewers left three mappings in four unchanged.
- Postgres holds every fact and all pipeline state; S3 only holds uploads and exports. The job table is the truth and queue messages are hints, so a duplicate or lost message is harmless.
- One job per open question with a lease, a fencing token and per-batch checkpoints: a dead worker's job resumes from its checkpoint; a zombie can't write on a stale fence.
- The fan-in is one transaction behind a row lock: mark the question done, flip the consultation if all are, queue the email. One finisher wins; the reconciler covers a crash.
- One answer row per respondent, question and option; a respondent snapshot makes 'villagers who cycle to work and oppose' one indexed query; nothing is edited in place, so counts reproduce.
- Responses are untrusted input: no tools, answers sent as JSON data, labels an enum of theme keys, small batches to bound the blast radius. Containment first; detection is a bonus.
- Encrypted at rest in London. Only the question, the open answer and any linked closed answer reach the model, via a gateway logging each job's inference region for the DPIA.
- Parity first, the manual process alongside and pilot consultations replayed as the gate. Then sign-off and dashboard, then self-serve behind department caps. A small team, two quarters.
- Model spend is small beside reviewer time, as the water commission press release shows; throughput and sign-off time bind. Cost is a guard: an estimate at Confirm, a department budget.
- Least sure, so I'd test first: the dashboard's query plan under a selective filter on the largest consultations, and whether per-question sign-off is bearable at dozens of questions.

I read i.AI's public repository and decision records first; where 'every department' changes the problem I've gone further.

---

I used Claude throughout, to research, to stress-test alternatives and to draft the diagrams. The choices are mine.
