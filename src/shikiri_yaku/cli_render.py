"""The output of the command line: doctor, stats and one line per failure, as text or as JSON.

Rich writes colour only to a terminal, so piped output is plain text. Soft wrapping keeps each
line whole, so a long detail is never split across lines of a pipe (design §3.1).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from rich.console import Console
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import timedelta

    from shikiri_yaku.app.stats import Measures

PASSED_GLYPH: Final = "✔"
FAILED_GLYPH: Final = "✘"
SEPARATOR: Final = " · "
BOLD: Final = "bold"
PASSED_COLOR: Final = "#90cba6"
FAILED_COLOR: Final = "#e3a0a0"
PRIMARY_TEXT_COLOR: Final = "#dce2e9"
SECONDARY_TEXT_COLOR: Final = "#9aa5b5"
SECTION_TITLE_COLOR: Final = "#83bddd"
MISROUTES_NOTE: Final = "Misroutes are a manual review (design §15) and are not printed"
_NONE: Final = "none"
_COLUMN_GAP: Final = 2


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One doctor check: a passed check says what it found; a failed one says what is wrong."""

    name: str
    detail: str
    passed: bool


def print_doctor(checks: Sequence[CheckResult]) -> None:
    """Print one line per check, a blank line, then the summary of all of them."""
    console = _console()
    for check in checks:
        console.print(_check_line(check))
    console.print()
    console.print(_summary(checks))


def print_json(document: Mapping[str, object]) -> None:
    """Print one JSON object on one line, to standard output."""
    sys.stdout.write(f"{json.dumps(document)}\n")


def doctor_document(checks: Sequence[CheckResult]) -> dict[str, object]:
    """Build the doctor report as JSON: the overall result, the counts and each check."""
    failed = _failures(checks)
    return {
        "passed": failed == 0,
        "failed": failed,
        "total": len(checks),
        "checks": [
            {"name": check.name, "passed": check.passed, "detail": check.detail} for check in checks
        ],
    }


def stats_document(measures: Measures) -> dict[str, object]:
    """Build the stats figures as JSON: durations in seconds, share null when unavailable."""
    return {
        "completed": measures.completed,
        "queued": measures.queued,
        "lifetime_seconds": measures.lifetime.total_seconds(),
        "waiting_seconds": measures.waiting.total_seconds(),
        "share": measures.share,
        "handled_without_po": dict(measures.handled_without_po),
        "reasons": dict(measures.reasons),
        "verdicts": dict(measures.verdicts),
        "installs": dict(measures.installs),
        "install_outcomes": dict(measures.install_outcomes),
    }


def print_failure(message: str) -> None:
    """Print a failed command's reason on one line, to standard error."""
    line = Text()
    line.append(FAILED_GLYPH, style=f"{BOLD} {FAILED_COLOR}")
    line.append(f" {_one_line(message)}", style=PRIMARY_TEXT_COLOR)
    _console(stderr=True).print(line)


@dataclass(frozen=True, slots=True)
class _Section:
    """A titled table of label and value rows, followed by the limits of its figures."""

    title: str
    rows: Sequence[tuple[str, str]]
    notes: Sequence[str] = ()


def print_stats(measures: Measures) -> None:
    """Print the figures of a journal as titled tables, each with the limits of its figures."""
    console = _console()
    for section in _sections(measures):
        console.print(Text(section.title, style=f"{BOLD} {SECTION_TITLE_COLOR}"))
        console.print(_table(section.rows))
        for note in section.notes:
            console.print(Text(note, style=SECONDARY_TEXT_COLOR))
        console.print()
    console.print(Text(MISROUTES_NOTE, style=SECONDARY_TEXT_COLOR))


def _sections(measures: Measures) -> list[_Section]:
    return [
        _Section(
            "1. PO wait time",
            [
                ("Completed escalations (resolved or closed)", str(measures.completed)),
                ("With an outbound question or approval", str(measures.queued)),
                ("Lifetime proxy", _seconds(measures.lifetime)),
                ("Queued waiting proxy", _seconds(measures.waiting)),
                ("Share waiting for the PO", _share(measures.share)),
            ],
            [
                "Journal-observed proxies, not Shikiru's exact times.",
                (
                    "Created is not always the original escalation time, closed is not always "
                    "resolved, and queued does not prove the PO received it."
                ),
            ],
        ),
        _Section(
            "2. Handled without the PO",
            _counted("category", measures.handled_without_po),
            [
                "Resolved, with no question or approval ever recorded.",
                "Closed signals are not counted; action attribution is unavailable.",
            ],
        ),
        _Section(
            "3. Escalation mix",
            [
                *_counted("reason", measures.reasons),
                *_counted("Reasoner verdict", measures.verdicts),
            ],
        ),
        _Section(
            "4. Installs: journaled outcomes",
            [
                *((name, str(count)) for name, count in measures.install_outcomes.items()),
                *_counted("current state", measures.installs),
            ],
            ["The current state is supplemental."],
        ),
    ]


def _counted(label: str, counts: Mapping[str, int]) -> list[tuple[str, str]]:
    if not counts:
        return [(f"{label}: {_NONE}", "")]
    return [(f"{label}: {key}", str(count)) for key, count in counts.items()]


def _table(rows: Sequence[tuple[str, str]]) -> Table:
    table = Table.grid(padding=(0, _COLUMN_GAP))
    table.add_column(style=PRIMARY_TEXT_COLOR, no_wrap=True)
    table.add_column(style=PRIMARY_TEXT_COLOR, justify="right", no_wrap=True)
    for label, value in rows:
        table.add_row(label, value)
    return table


def _seconds(duration: timedelta) -> str:
    return f"{round(duration.total_seconds())} s"


def _share(share: float | None) -> str:
    return "unavailable" if share is None else f"{share:.1%}"


def _console(*, stderr: bool = False) -> Console:
    return Console(stderr=stderr, soft_wrap=True, markup=False, highlight=False, emoji=False)


def _check_line(check: CheckResult) -> Text:
    line = _outcome(passed=check.passed)
    line.append(f" {_title(check.name)}", style=f"{BOLD} {PRIMARY_TEXT_COLOR}")
    line.append(SEPARATOR, style=SECONDARY_TEXT_COLOR)
    line.append(_one_line(check.detail), style=PRIMARY_TEXT_COLOR)
    return line


def _failures(checks: Sequence[CheckResult]) -> int:
    return sum(not check.passed for check in checks)


def _summary(checks: Sequence[CheckResult]) -> Text:
    failed = _failures(checks)
    if failed == 0:
        line = _outcome(passed=True)
        line.append(" Doctor passed", style=f"{BOLD} {PASSED_COLOR}")
        count = f"{len(checks)} checks"
    else:
        line = _outcome(passed=False)
        line.append(" Doctor failed", style=f"{BOLD} {FAILED_COLOR}")
        count = f"{failed} of {len(checks)} checks"
    line.append(SEPARATOR, style=SECONDARY_TEXT_COLOR)
    line.append(count, style=PRIMARY_TEXT_COLOR)
    return line


def _outcome(*, passed: bool) -> Text:
    glyph, color = (PASSED_GLYPH, PASSED_COLOR) if passed else (FAILED_GLYPH, FAILED_COLOR)
    line = Text()
    line.append(glyph, style=f"{BOLD} {color}")
    return line


def _title(name: str) -> str:
    return name[:1].upper() + name[1:]


# keep: settings joins its problems with newlines; each error and check must stay on one line
def _one_line(text: str) -> str:
    lines = (line.strip() for line in text.splitlines())
    return "; ".join(line for line in lines if line)
