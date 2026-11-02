"""The Restart action: starts a stopped or faulted engine once, then confirms it (§6.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.loop_guard import Refused
from shikiri_yaku.domain.outcome import Forward, Informed
from shikiri_yaku.domain.signal import Kind
from shikiri_yaku.ports import AttemptState, CommandError, ShikiruRejectedError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import timedelta

    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import Clock, DoctorCheck, Guard, Notifier, ShikiruClient, StartLog

RESTART_REASONS: Final = frozenset({Kind.ENGINE_DOWN.value, Kind.ENGINE_FAULT.value})
RESTARTED: Final = "The engine was restarted and is up."
EARLIER_FAILED: Final = "An earlier start of the engine for this signal failed."
NOT_UP: Final = "The engine is still not up when the confirm time is over."


class RestartFailedError(Exception):
    """A start Shikiru refused or did not finish; the message carries the failed checks."""


class RestartAction:
    """Starts a stopped or faulted engine once, then waits until `status().engine_up` confirms it.

    The start is journaled as issued before Shikiru's `start` runs, and the loop guard counts it
    when it grants the restart, so an unconfirmed start still spends the budget. A signal whose
    start is already journaled never starts the engine again: the action only waits for the
    engine, and forwards if the start failed. A start Shikiru refuses is failed in the journal and
    raised with Shikiru's message, so the Router forwards it.
    """

    reasons = RESTART_REASONS

    def __init__(
        self,
        clients: Mapping[str, ShikiruClient],
        guard: Guard,
        starts: StartLog,
        *,
        notifier: Notifier,
        clock: Clock,
        confirm_within: timedelta,
        poll: timedelta,
    ) -> None:
        self._clients = clients
        self._guard = guard
        self._starts = starts
        self._notifier = notifier
        self._clock = clock
        self._confirm_within = confirm_within
        self._poll = poll

    async def run(self, signal: Signal) -> Outcome:
        existing = self._starts.existing(signal)
        if existing is not None:
            if existing.state == AttemptState.FAILED:
                return Forward(await self._give_up(signal.project, EARLIER_FAILED))
            return await self._confirm(signal)
        decision = self._guard.restart(signal.project)
        if isinstance(decision, Refused):
            return Forward(await self._give_up(signal.project, decision.note))
        self._starts.issue(signal, self._clock.now())
        await self._start(signal)
        return await self._confirm(signal)

    async def _start(self, signal: Signal) -> None:
        try:
            await self._clients[signal.project].start_engine()
        except ShikiruRejectedError as error:
            self._starts.fail(signal, self._clock.now())
            note = await self._give_up(signal.project, f"Shikiru refused the start: {error}")
            raise RestartFailedError(note) from error
        # keep: an unknown outcome stays issued, so the next look at the op decides
        except CommandError as error:
            note = await self._give_up(signal.project, f"The start did not run to its end: {error}")
            raise RestartFailedError(note) from error

    async def _confirm(self, signal: Signal) -> Outcome:
        deadline = self._clock.now() + self._confirm_within
        while not await self._engine_up(signal.project):
            now = self._clock.now()
            if now >= deadline:
                self._starts.fail(signal, now)
                return Forward(await self._give_up(signal.project, NOT_UP))
            await self._clock.sleep(min(self._poll, deadline - now).total_seconds())
        self._starts.confirm(signal, self._clock.now())
        self._notifier.tell(signal, _notice(signal.project), key=f"restart:{signal.id}")
        return Informed(RESTARTED)

    async def _engine_up(self, project: str) -> bool:
        return (await self._clients[project].status()).engine_up

    async def _give_up(self, project: str, *sentences: str) -> str:
        # keep: every forward leaves the engine down, so each one tells the PO what to do next
        failed = await self._clients[project].doctor()
        instruction = f"Please start it yourself with `shikiru start` in the {project} project."
        return _join(*sentences, _checks(failed), instruction)


def _join(*sentences: str) -> str:
    # keep: a sentence that already ends in a stop, such as one Shikiru wrote, gets only one
    return " ".join(f"{sentence.rstrip('.')}." for sentence in sentences if sentence)


def _checks(failed: Sequence[DoctorCheck]) -> str:
    if not failed:
        return ""
    described = "; ".join(f"{check.name} ({check.detail})" for check in failed)
    return f"The failed checks are: {described}"


def _notice(project: str) -> str:
    return f"The engine of {project} was restarted. Shikiru resumes its tasks."
