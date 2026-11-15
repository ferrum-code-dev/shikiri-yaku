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
from typing import TYPE_CHECKING, Final, cast

from shikiri_yaku.ports import AdminCommandResult, CommandError, CommandResult
from shikiri_yaku.settings import ADMIN_OUTPUT_READ_BYTES

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
        stdin: str | None = None,
    ) -> CommandResult:
        merged = None if env is None else {**os.environ, **env}
        # keep: files, not pipes: after a kill, a pipe may keep unread output or block forever
        with (
            tempfile.TemporaryFile() as stdout,
            tempfile.TemporaryFile() as stderr,
            tempfile.TemporaryFile() as input_file,
        ):
            if stdin is not None:
                input_file.write(stdin.encode())
                input_file.seek(0)
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL if stdin is None else input_file,
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

    async def run_admin(  # noqa: C901 -- capture, timeout, and redaction share state
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path,
        max_bytes: int,
        secrets: Sequence[str],
    ) -> AdminCommandResult:
        values = tuple(value.encode() for value in secrets if value)
        lookahead = max((len(value) for value in values), default=1) - 1
        captured = bytearray()
        total = 0
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                cwd=cwd,
            )
        except OSError:
            return AdminCommandResult(returncode=None, output="", cut=False)

        async def drain() -> None:
            nonlocal total
            if process.stdout is None:
                return
            while chunk := await process.stdout.read(ADMIN_OUTPUT_READ_BYTES):
                total += len(chunk)
                captured.extend(chunk[: max(0, max_bytes + lookahead - len(captured))])

        reader = asyncio.create_task(drain())
        try:
            await asyncio.wait_for(_wait_for_group(process), timeout_s)
        except TimeoutError:
            _close_admin_pipe(process)
            await _kill_group(process)
            code = None
        except asyncio.CancelledError:
            _close_admin_pipe(process)
            await _kill_group(process)
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
            raise
        else:
            code = process.returncode
        await reader
        raw = bytes(captured)
        masked = bytearray(raw[:max_bytes])
        for value in values:
            start = 0
            while (index := raw.find(value, start)) != -1:
                end = min(index + len(value), max_bytes)
                if index < end:
                    masked[index:end] = b"*" * (end - index)
                start = index + 1
        return AdminCommandResult(code, masked.decode(errors="replace"), total > max_bytes)


def _close_admin_pipe(process: asyncio.subprocess.Process) -> None:
    # keep: a process outside the group may still hold the pipe after the group is killed
    if process.stdout is not None:
        cast("asyncio.ReadTransport", getattr(process.stdout, "_transport")).close()  # noqa: B009


async def _kill_group(process: asyncio.subprocess.Process) -> None:
    # keep: a child that ignores SIGTERM, and the children it started, die with the group
    # keep: on macOS a group of exited, unreaped processes answers EPERM, as in _group_exists
    with contextlib.suppress(ProcessLookupError, PermissionError):
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
    # keep: macOS answers EPERM for a group of exited, unreaped processes; the group is ours
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError, PermissionError:
        return False
    return True


def _read(output: BinaryIO) -> str:
    output.seek(0)
    return output.read().decode(errors="replace")
