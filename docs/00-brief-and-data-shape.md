# The brief and the data shape, in my words

Everything here is paraphrased from the brief and from the layout of the two
example files (sheet names, headers and the two markers), checked on 19
September 2026. Nothing is quoted, and no example row appears anywhere in this
repository (`CLAUDE.md`, rule 1). Where a line records a choice of mine rather
than a fact, it says so.

## The task

Design the architecture for a production version of a consultation-analysis
service. The service reads the responses to a public consultation, proposes a
set of themes for each open question, and tags every response with the themes
it matches. A piloted version exists; the design is for the version every
department could rely on, for every consultation it runs.

The deliverable is one or two diagrams of whatever matters most to the design,
plus a short written explanation. Authentication is out of scope.

## The user journey, in four steps

1. A policy team uploads a spreadsheet of responses into a web app.
2. They configure the shape of the consultation and its questions in the app.
3. They wait, and get an email when the analysis is ready.
4. They explore a dashboard: read the responses to each question, see the
   themes the model proposed, and filter by demographic group or by the answers
   to other questions.

Step 2 is the one I keep coming back to. The last section says why.

## What runs today

A policy team sends two spreadsheets: the responses and the question
definitions. The analysis team puts them in S3 and runs a local script once per
open question. The script sends every response to that question to the model
and gets themes back; it then sends each response again and gets its tags back;
both land in S3. What goes back to the policy team is a report prepared by
hand, with tables and graphs of theme frequency per question, and a copy of the
responses spreadsheet with the themes appended.

I read that as: today the pipeline is a script plus one person's judgement.
The design has to keep the second while taking away the manual work.

## The responses file

One row per respondent, one column per question. Demographic questions are
ordinary columns alongside the rest.

- A multi-select answer sits in one cell, the chosen options joined by commas.
- `-` means the respondent gave no answer.
- `N/A` appears on demographic columns that don't apply to every respondent
  and means "not applicable". It's a real value someone will want to filter
  on, and it isn't the same as no answer. My choice: how `N/A` is treated is
  set per question at configure time, as a value policy, rather than fixed in
  code (`docs/02-architecture.md`).
- One column is an identifier for the response. It isn't a question, and it
  appears in none of the definition sheets. So the configure step needs a role
  for any column the workbook doesn't describe: respondent id, identity to keep
  out of the pipeline, or ignore (`docs/02-architecture.md`).

## The definition workbook

Three sheets, each with its own headers.

| Sheet | Headers |
|---|---|
| Demographic questions | `column_reference`, `question_text` |
| Closed questions | `column_reference`, `question_text`, `response_type`, `options` |
| Open questions | `column_reference`, `question_text`, `related_closed_column` |

- `column_reference` is how a sheet names a column in the responses file.
- `response_type` names the closed question's type; the validator accepts a
  fixed vocabulary of three (`docs/02-architecture.md`, 3.2).
- `options` lists the allowed answers to a closed question, joined by commas.
- `related_closed_column` ties an open question to the closed question it
  follows up. `-` or blank means there's no related question. Where there is
  one, the open question's text carries a placeholder that stands for the
  respondent's own answer to that closed question, so the wording each
  respondent saw depended on what they'd already answered. The pipeline has to
  carry that answer alongside the open response, or the model reads a question
  with a hole in it (`docs/02-architecture.md`).

## The one observation that matters

`options` is a comma-joined string. An option that contains a comma can't be
written in this format at all: split on the comma and it becomes two options.
I could invent an escaping rule, and nobody sending a spreadsheet would know
to use it.

That's the argument for step 2 of the journey. Configuration lives in
the app, where a question's type and its options are data with a proper shape.
The workbook is an optional importer that pre-fills the form and lists its
warnings, and the person configuring the consultation resolves those warnings
before anything is spent on the model. `-` and `N/A` get the same treatment:
the importer recognises them as conventions, and the app records the decision
per question. The workbook describes the consultation. It shouldn't be the
thing the service trusts.

Where this goes next: `docs/02-architecture.md` for the configure step and the
validator, `docs/04-data-model.md` for the tables the configuration lands in.
