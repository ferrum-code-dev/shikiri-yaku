"""Reconciles issued resolves by observation: after a restart and when Shikiru loses one (§10.1)."""

from __future__ import annotations

import contextlib
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from shikiri_yaku.ports import (
    AckStatus,
    EscalationStatus,
    EventKinds,
    ResolutionKind,
    ShikiruError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from shikiri_yaku.ports import (
        Clock,
        LifecycleJournal,
        ReconcileLog,
        Reshower,
        ShikiruClient,
        Unsettled,
    )

AGAIN_NOTE: Final = "Your answer did not reach Shikiru, so please answer again."
RETRY_AGAIN_NOTE: Final = "The automatic retry did not reach Shikiru, so the decision is yours."


async def is_lost(
    lifecycles: LifecycleJournal,
    client: ShikiruClient,
    project: str,
    *,
    since: datetime,
    now: datetime,
    lost_after: timedelta,
) -> bool:
    """Whether a queued command is lost: the engine is up, and it waited `lost_after` for it."""
    state = lifecycles.state(project)
    lifecycle = None if state is None else state.lifecycle
    if lifecycle is None or lifecycle.kind != EventKinds.ENGINE_STARTED:
        return False
    if not (await client.status()).engine_up:
        return False
    return now >= max(lifecycle.at, since) + lost_after


class ResolveReconciler:
    """Settles every issued resolve whose result the events did not confirm.

    An op with no acknowledgement when the Operator starts is uncertain: the escalation's state
    decides. A queued one is lost when its escalation is still open `lost_command_min` after the
    engine is up again. A lost or uncertain op fails and the question is shown again.
    """

    def __init__(
        self,
        log: ReconcileLog,
        lifecycles: LifecycleJournal,
        reshower: Reshower,
        clients: Mapping[str, ShikiruClient],
        clock: Clock,
        *,
        lost_command_min: int,
        interval_s: float,
    ) -> None:
        self._log = log
        self._lifecycles = lifecycles
        self._reshower = reshower
        self._clients = clients
        self._clock = clock
        self._lost_after = timedelta(minutes=lost_command_min)
        self._interval_s = interval_s

    async def run(self) -> None:
        # keep: taken before anything else: an op issued later by this process is not uncertain
        uncertain = {op.op_id for op in self._log.unsettled() if op.ack is None}
        while True:
            await self.tick(uncertain)
            await self._clock.sleep(self._interval_s)

    async def tick(self, uncertain: set[int]) -> None:
        for op in self._log.unsettled():
            # keep: the link supervisor reports an outage; this op is tried again on the next tick
            with contextlib.suppress(ShikiruError):
                await self._settle(op, uncertain)

    async def _settle(self, op: Unsettled, uncertain: set[int]) -> None:
        if op.ack is None:
            if op.op_id in uncertain:
                await self._decide(op, lost=True)
                uncertain.discard(op.op_id)
        elif op.ack.status == AckStatus.QUEUED:
            await self._decide(op, lost=await self._is_lost(op))

    async def _decide(self, op: Unsettled, *, lost: bool) -> None:
        escalation_id = op.payload.escalation_id
        view = await self._clients[op.payload.project].get_escalation(escalation_id)
        if view is None:
            return
        now = self._clock.now()
        if view.escalation.status != EscalationStatus.OPEN:
            # keep: a queued command's event confirms it; without an acknowledgement there is no id
            if op.ack is None and view.escalation.resolved_as(op.payload.kind):
                self._log.confirm(op.op_id, now)
            elif op.ack is None:
                self._log.fail(op.op_id, now)
        elif lost:
            note = RETRY_AGAIN_NOTE if op.payload.kind == ResolutionKind.RETRY else AGAIN_NOTE
            await self._reshower.reshow(
                op.payload.number, op.signal, note, key=f"reshow:{op.op_id}"
            )
            self._log.fail(op.op_id, now, question_shown=True)

    async def _is_lost(self, op: Unsettled) -> bool:
        project = op.payload.project
        return await is_lost(
            self._lifecycles,
            self._clients[project],
            project,
            since=op.since,
            now=self._clock.now(),
            lost_after=self._lost_after,
        )
