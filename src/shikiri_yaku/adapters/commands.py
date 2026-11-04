"""The CommandRunner on this host: one argument list, no shell, and the group killed on early exit.

A timeout or a cancelled call kills the group and reaps the leader, so no call leaves a process
running.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import tempfile
from typing import TYPE_CHECKING, Final

from shikiri_yaku.ports import CommandError, CommandResult

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path
    from typing import BinaryIO

_GROUP_POLL_S: Final = 0.02


class ProcessCommandRunner:
    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        merged = None if env is None else {**os.environ, **env}
        # keep: files, not pipes: after a kill, a pipe may keep unread output or block forever
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                    cwd=cwd,
                    env=merged,
                )
            except OSError as error:
                message = f"{argv[0]} could not be started: {error}"
                raise CommandError(message) from error
            try:
                await asyncio.wait_for(_wait_for_group(process), timeout_s)
            except TimeoutError as error:
                await _kill_group(process)
                message = f"{argv[0]} did not finish in {timeout_s:g} seconds and was killed"
                raise CommandError(message, output=_read(stdout) + _read(stderr)) from error
            except asyncio.CancelledError:
                await _kill_group(process)
                raise
            return CommandResult(
                returncode=process.returncode if process.returncode is not None else -1,
                stdout=_read(stdout),
                stderr=_read(stderr),
            )


async def _kill_group(process: asyncio.subprocess.Process) -> None:
    # keep: a child that ignores SIGTERM, and the children it started, die with the group
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()
    await _wait_for_group_exit(process.pid)


async def _wait_for_group(process: asyncio.subprocess.Process) -> None:
    await process.wait()
    await _wait_for_group_exit(process.pid)


async def _wait_for_group_exit(group_id: int) -> None:
    while _group_exists(group_id):
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.Event().wait(), _GROUP_POLL_S)


def _group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    return True


def _read(output: BinaryIO) -> str:
    output.seek(0)
    return output.read().decode(errors="replace")
