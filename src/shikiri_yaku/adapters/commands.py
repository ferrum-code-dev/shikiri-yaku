"""The CommandRunner on this host: one argument list, no shell, killed as a group on timeout."""

from __future__ import annotations

import asyncio
import os
import signal
from typing import TYPE_CHECKING

from shikiri_yaku.ports import CommandError, CommandResult

if TYPE_CHECKING:
    from collections.abc import Sequence


class ProcessCommandRunner:
    async def run(self, argv: Sequence[str], *, timeout_s: float) -> CommandResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            message = f"{argv[0]} could not be started: {error}"
            raise CommandError(message) from error
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_s)
        except TimeoutError as error:
            # keep: a child that ignores SIGTERM, and the children it started, die with the group
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            message = f"{argv[0]} did not finish in {timeout_s:g} seconds and was killed"
            raise CommandError(message) from error
        return CommandResult(
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )
