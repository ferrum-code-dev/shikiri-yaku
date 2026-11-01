"""Runs an approved install op (design §6.3, steps 4 to 6): the stored command, then the check.

The op is claimed under the host lock, before its command runs, so a second run for the same op
finds nothing to claim, and an approval whose escalation was resolved elsewhere is void. The lock
covers the claim and the command only: the check reads the host and needs no turn. A run the check
does not confirm is forwarded to the PO with the tail of its output. The forward is retried in this
process while the op stays issued, and only then does the op end `failed`; an op a restart finds
issued is settled by its check (design §10). A passing check leaves its op issued until the
`escalation_resolved` event confirms it.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class _Run:
    text: str
    failure: str | None


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
        forward_retry_s: float,
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
        self._retry_s = forward_retry_s
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
        """Claim the op and run its stored command under the host lock; then check it.

        Nothing runs when the claim is lost, and a claim is lost when the op is no longer approved
        or its escalation is no longer forwarded. A lost link or a protocol error leaves the op
        `issued`: the answer may have reached Shikiru, so the next start decides by the check
        whether it is still owed (design §10.1).
        """
        async with self._host.hold():
            claim = self._log.claim(op_id, self._clock.now())
            if claim is None:
                return
            run = await self._run(claim.op.payload.argv)
        await self._settle(claim, run)

    async def _run(self, argv: tuple[str, ...]) -> _Run:
        command = [self._shell, "-lc", shlex.join(argv)]
        try:
            result = await self._commands.run(
                command,
                timeout_s=self._timeout_s,
                cwd=self._files.scratch_dir(),
                env=NONINTERACTIVE_ENV,
            )
        except CommandError as error:
            message = f"the command did not run to its end: {error}"
            return _Run(error.output, message)
        text = result.stdout + result.stderr
        if result.returncode != 0:
            return _Run(text, f"the command exited with status {result.returncode}")
        return _Run(text, None)

    async def _settle(self, claim: InstallClaim, run: _Run) -> None:
        if run.failure is not None:
            await self._fail(claim, run.text, run.failure)
            return
        tool = claim.op.payload.tool
        try:
            found = await self._check.find(tool)
        except (CommandError, ToolNameError) as error:
            message = f"the check after the run did not run to its end: {error}"
            await self._fail(claim, run.text, message)
            return
        if found is None:
            await self._fail(claim, run.text, f"the check after the run did not find {tool}")
            return
        await self._answer(claim, run.text, found)

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
        note = f"{reason}. Output tail:\n{_tail(text, self._tail_lines)}"
        await self._forward_until_queued(claim, note)
        self._log.fail(claim.op.id, path, self._clock.now())

    async def _forward_until_queued(self, claim: InstallClaim, note: str) -> None:
        # keep: the op stays issued until the PO has the failure, so a restart can find it
        while True:
            try:
                await self._forwarder.forward(
                    claim.op.payload.number, claim.signal, note, claim.suggestion
                )
            except Exception as error:  # noqa: BLE001 -- the Channel may fail in any way
                _LOG.warning(
                    "forwarding an install failure failed, trying again in %g s: %s",
                    self._retry_s,
                    error,
                )
                await self._clock.sleep(self._retry_s)
            else:
                return

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _LOG.error("an install run stopped unexpectedly", exc_info=error)


def _tail(text: str, lines: int) -> str:
    return "\n".join(text.splitlines()[-lines:])
