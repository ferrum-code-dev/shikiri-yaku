"""Starts the commands of an update and waits between checks; the only module that does either."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from shikiri_yaku.update.host import FAILED, CommandResult

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def command_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Return the environment for a command, without the checkout's virtualenv on PATH.

    `uv run` puts the checkout's `.venv/bin` first, so a bare `shikiri-yaku` would be the checkout's
    copy, not the installed release that the verification must check.
    """
    env = dict(environ)
    venv = environ.get("VIRTUAL_ENV")
    if venv:
        bin_dir = str(Path(venv) / "bin")
        entries = environ.get("PATH", "").split(os.pathsep)
        env["PATH"] = os.pathsep.join(entry for entry in entries if entry != bin_dir)
    return env


def run_command(argv: Sequence[str]) -> CommandResult:
    sys.stdout.write(f"$ {shlex.join(argv)}\n")
    try:
        done = subprocess.run(
            argv,
            env=command_environment(os.environ),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        return CommandResult(FAILED, "", str(error))
    return CommandResult(done.returncode, done.stdout, done.stderr)


def pause(seconds: float) -> None:
    time.sleep(seconds)
