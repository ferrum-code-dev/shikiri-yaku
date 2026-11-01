"""Runs an approved install op (design §6.3, steps 4 to 6): the stored command, then the check.

The op is claimed before any host command runs, so a second run for the same op finds nothing to
claim. The host lock covers the command only: the check reads the host and needs no turn. A run the
check does not confirm is forwarded to the PO with the tail of its output, and its op ends `failed`.
A passing check leaves its op issued until the `escalation_resolved` event confirms it.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from shikiri_yaku.app.tool_check import ToolNameError
from shikiri_yaku.ports import (
    CommandError,
    ResolutionKind,
    ShikiruError,
    ShikiruRejectedError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.ports import (
        Clock,
        CommandResult,
        CommandRunner,
        Forwarder,
        HostLock,
        InstallClaim,
        InstallFiles,
        InstallRunLog,
        InstallToolCheck,
        ShikiruClient,
    )

_LOG = logging.getLogger(__name__)
# keep: installers read these to stay non-interactive; the runner closes stdin itself
NONINTERACTIVE_ENV: Final = MappingProxyType({"CI": "1", "NONINTERACTIVE": "1"})
ANSWER: Final = "Installed {tool} with {manager} (found at {path}). Please continue."
FINAL_LINE: Final = "Installed {tool} with {manager}."


class _RunError(Exception):
    """A run that the check does not confirm; `text` is the output it left, possibly empty."""

    def __init__(self, reason: str, text: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.text = text


class InstallExecutor:
    """Runs approved install ops, each in a task of its own, and settles every run with the PO."""

    def __init__(
        self,
        log: InstallRunLog,
        clients: Mapping[str, ShikiruClient],
        forwarder: Forwarder,
        check: InstallToolCheck,
        commands: CommandRunner,
        *,
        host: HostLock,
        files: InstallFiles,
        clock: Clock,
        shell: str,
        timeout_s: float,
        tail_lines: int,
    ) -> None:
        self._log = log
        self._clients = clients
        self._forwarder = forwarder
        self._check = check
        self._commands = commands
        self._host = host
        self._files = files
        self._clock = clock
        self._shell = shell
        self._timeout_s = timeout_s
        self._tail_lines = tail_lines
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, op_id: int) -> None:
        """Run the op in a task of its own; the call returns before the command starts."""
        task = asyncio.ensure_future(self.execute(op_id))
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    async def join(self) -> None:
        """Wait until every run started so far has ended."""
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def execute(self, op_id: int) -> None:
        """Claim the op, run its stored command and check it; do nothing when the claim is lost.

        A lost link or a protocol error leaves the op `issued`: the answer may have reached Shikiru,
        so the next start decides by the check whether it is still owed (design §10.1).
        """
        claim = self._log.claim(op_id, self._clock.now())
        if claim is None:
            return
        try:
            text = await self._run(claim.op.payload.argv)
            found = await self._find(claim.op.payload.tool, text)
        except _RunError as error:
            await self._fail(claim, error.text, error.reason)
            return
        await self._answer(claim, text, found)

    async def _run(self, argv: tuple[str, ...]) -> str:
        try:
            result = await self._locked_run(argv)
        except CommandError as error:
            message = f"the command did not run to its end: {error}"
            raise _RunError(message, str(error)) from error
        text = result.stdout + result.stderr
        if result.returncode != 0:
            message = f"the command exited with status {result.returncode}"
            raise _RunError(message, text)
        return text

    async def _locked_run(self, argv: tuple[str, ...]) -> CommandResult:
        command = [self._shell, "-lc", shlex.join(argv)]
        async with self._host.hold():
            return await self._commands.run(
                command,
                timeout_s=self._timeout_s,
                cwd=self._files.scratch_dir(),
                env=NONINTERACTIVE_ENV,
            )

    async def _find(self, tool: str, text: str) -> str:
        try:
            found = await self._check.find(tool)
        except (CommandError, ToolNameError) as error:
            message = f"the check after the run did not run to its end: {error}"
            raise _RunError(message, text) from error
        if found is None:
            message = f"the check after the run did not find {tool}"
            raise _RunError(message, text)
        return found

    async def _answer(self, claim: InstallClaim, text: str, found: str) -> None:
        operation = claim.op
        payload = operation.payload
        path = self._files.save_output(operation.id, text)
        answer = ANSWER.format(tool=payload.tool, manager=payload.manager, path=found)
        try:
            ack = await self._clients[operation.project].resolve_escalation(
                payload.escalation_id, ResolutionKind.ANSWER, answer
            )
        except ShikiruRejectedError as error:
            await self._stop(claim, path, text, f"Shikiru rejected the answer: {error}")
            return
        except ShikiruError:
            return
        final = FINAL_LINE.format(tool=payload.tool, manager=payload.manager)
        self._log.acknowledge(operation.id, ack, path, final, self._clock.now())

    async def _fail(self, claim: InstallClaim, text: str, reason: str) -> None:
        await self._stop(claim, self._files.save_output(claim.op.id, text), text, reason)

    async def _stop(self, claim: InstallClaim, path: str, text: str, reason: str) -> None:
        if not self._log.fail(claim.op.id, path, self._clock.now()):
            return
        note = f"{reason}. Output tail:\n{_tail(text, self._tail_lines)}"
        await self._forwarder.forward(claim.op.payload.number, claim.signal, note, claim.suggestion)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _LOG.error("an install run stopped unexpectedly", exc_info=error)


def _tail(text: str, lines: int) -> str:
    return "\n".join(text.splitlines()[-lines:])
