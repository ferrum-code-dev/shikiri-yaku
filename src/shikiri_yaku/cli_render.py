"""The human output of the command line: doctor's lines and summary, and one line per failure.

Rich writes colour only to a terminal, so piped output is plain text. Soft wrapping keeps each
line whole, so a long detail is never split across lines of a pipe (design §3.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from rich.console import Console
from rich.text import Text

if TYPE_CHECKING:
    from collections.abc import Sequence

PASSED_GLYPH: Final = "✔"
FAILED_GLYPH: Final = "✘"
SEPARATOR: Final = " · "
BOLD: Final = "bold"
PASSED_COLOR: Final = "#90cba6"
FAILED_COLOR: Final = "#e3a0a0"
PRIMARY_TEXT_COLOR: Final = "#dce2e9"
SECONDARY_TEXT_COLOR: Final = "#9aa5b5"


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


def print_failure(message: str) -> None:
    """Print a failed command's reason on one line, to standard error."""
    line = Text()
    line.append(FAILED_GLYPH, style=f"{BOLD} {FAILED_COLOR}")
    line.append(f" {message}", style=PRIMARY_TEXT_COLOR)
    _console(stderr=True).print(line)


def _console(*, stderr: bool = False) -> Console:
    return Console(stderr=stderr, soft_wrap=True, markup=False, highlight=False, emoji=False)


def _check_line(check: CheckResult) -> Text:
    line = _outcome(passed=check.passed)
    line.append(f" {_title(check.name)}", style=f"{BOLD} {PRIMARY_TEXT_COLOR}")
    line.append(SEPARATOR, style=SECONDARY_TEXT_COLOR)
    line.append(check.detail, style=PRIMARY_TEXT_COLOR)
    return line


def _summary(checks: Sequence[CheckResult]) -> Text:
    failed = sum(not check.passed for check in checks)
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
