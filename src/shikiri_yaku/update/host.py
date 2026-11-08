"""What the update steps share: the command results, the places on the host, and the runner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from shikiri_yaku.settings import UpdateSettings

PROGRAM: Final = "shikiri-yaku"
DISTRIBUTION: Final = "shikiri_yaku"
FAILED: Final = 1


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


type Runner = Callable[[Sequence[str]], CommandResult]
type Pause = Callable[[float], None]


@dataclass(frozen=True)
class Layout:
    """Where the host keeps releases, and the home directory of the products."""

    store: Path
    home: Path


@dataclass(frozen=True)
class Host:
    """Everything an update step needs: the runner, the pause, the places and the numbers."""

    run: Runner
    pause: Pause
    layout: Layout
    settings: UpdateSettings


class UpdateError(Exception):
    """A step failed before anything was installed, so there is nothing to roll back."""


def output_tail(result: CommandResult, lines: int) -> str:
    text = (result.stdout + result.stderr).strip().splitlines()
    return "\n".join(text[-lines:]) or f"exit status {result.returncode}"
