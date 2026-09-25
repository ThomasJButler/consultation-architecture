"""The validator's report, rendered for a terminal or as JSON.

What it prints is what the configure screen shows (docs/02, step 3):
column names, counts, row numbers, the distinct values of demographic and
closed columns, and the warnings with their resolutions. An open answer is
never in it, because the report never holds one.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from consult.definition import Definition
from consult.validate import ColumnKind, Report, Warning


def _resolutions(warning: Warning) -> str:
    return ", ".join(
        f"{resolution.value} (default)" if resolution is warning.default else resolution.value
        for resolution in warning.resolutions
    )


def _warning_line(warning: Warning) -> str:
    what = ""
    if warning.value is not None:
        what = f'"{warning.value}" x{warning.count}'
        if warning.example_rows:
            what += ", rows " + ", ".join(str(no) for no in warning.example_rows)
        what += ": "
    return f"  {warning.kind.value:<24}{warning.column_ref:<16}{what}{_resolutions(warning)}"


def render(report: Report, responses: Path, definition: Definition, workbook: Path) -> str:
    lines = [
        f"{responses.name}: {report.row_count} rows, {len(report.columns)} columns. "
        f"{workbook.name}: {len(definition.demographic)} demographic, "
        f"{len(definition.closed)} closed, {len(definition.open)} open questions."
    ]
    if report.errors:
        lines.append(f"errors: {len(report.errors)}")
        lines.extend(f"  {error}" for error in report.errors)
    else:
        lines.append("errors: none")
    lines.append(f"warnings: {len(report.warnings)}")
    lines.extend(_warning_line(warning) for warning in report.warnings)
    lines.append("columns:")
    for column in report.columns:
        line = f"  {column.column_ref:<16}{column.kind.value:<13}"
        if column.kind is not ColumnKind.UNMATCHED:
            line += f"answered {column.answered}, not answered {column.not_answered}"
        if column.values:
            line += ": " + ", ".join(f"{value} {count}" for value, count in column.values)
        lines.append(line)
    estimate = report.estimate
    lines.append(
        f"open answers: {report.open_answer_count}; ~{estimate.tokens_total:,} tokens; "
        f"~{estimate.pence_cached}p cached, ~{estimate.pence_uncached}p uncached"
    )
    lines.extend(f"  {assumption}" for assumption in estimate.assumptions)
    return "\n".join(lines)


def as_json(report: Report) -> str:
    estimate = report.estimate
    document = {
        **asdict(report),
        "estimate": {
            **asdict(estimate),
            "tokens_total": estimate.tokens_total,
            "pence_cached": estimate.pence_cached,
            "pence_uncached": estimate.pence_uncached,
            "assumptions": list(estimate.assumptions),
        },
    }
    return json.dumps(document, indent=2)
