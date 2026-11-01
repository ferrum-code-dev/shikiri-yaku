"""Settles unsettled installs by observation (design §6.3 step 7, §10).

An approved op is checked against Shikiru before it runs. An issued op is decided by its escalation,
then by its check; the host install is never repeated. A tool found is answered, and a missing tool
is asked again. An answer the lost link did not deliver is shown to the PO again. Each pass observes
unsettled ops again, while an op a run of this process has in hand is left alone.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
from typing import TYPE_CHECKING

from shikiri_yaku.app.install_executor import final_line, send_answer
from shikiri_yaku.app.reconciler import AGAIN_NOTE, is_lost
from shikiri_yaku.app.tool_check import ToolNameError
from shikiri_yaku.ports import (
    INSTALL_APPROVED,
    AckStatus,
    CommandError,
    EscalationStatus,
    ResolutionKind,
    ShikiruError,
    ShikiruRejectedError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from shikiri_yaku.app.install_reask import InstallReasker
    from shikiri_yaku.ports import (
        Clock,
        EscalationView,
        InstallReconcileLog,
        InstallRunner,
        InstallToolCheck,
        LifecycleJournal,
        PendingInstall,
        Reshower,
        ShikiruClient,
    )


class InstallReconciler:
    """Repeatedly observes approved and issued installs to settle or resume recovery."""

    def __init__(
        self,
        log: InstallReconcileLog,
        lifecycles: LifecycleJournal,
        clients: Mapping[str, ShikiruClient],
        runner: InstallRunner,
        *,
        reshower: Reshower,
        check: InstallToolCheck,
        reasker: InstallReasker,
        clock: Clock,
        lost_command_min: int,
        interval_s: float,
    ) -> None:
        self._log = log
        self._lifecycles = lifecycles
        self._reshower = reshower
        self._clients = clients
        self._runner = runner
        self._check = check
        self._reasker = reasker
        self._clock = clock
        self._lost_after = timedelta(minutes=lost_command_min)
        self._interval_s = interval_s

    async def run(self) -> None:
        while True:
            await self.tick()
            await self._clock.sleep(self._interval_s)

    async def tick(self) -> None:
        for pending in self._log.unsettled():
            op_id = pending.op.id
            if self._runner.running(op_id):
                continue
            # keep: a check or a link that fails leaves the op for the next start to decide
            with contextlib.suppress(ShikiruError, CommandError, ToolNameError):
                await self._settle(op_id)

    async def _settle(self, op_id: int) -> None:
        pending = self._log.unsettled_op(op_id)
        if pending is None:
            return
        if pending.rejection is not None:
            await self._deliver_rejection(pending, pending.rejection)
            return
        client = self._clients.get(pending.op.project)
        if client is None:
            if pending.op.state == INSTALL_APPROVED:
                self._log.void_approved(op_id, self._clock.now())
            else:
                self._log.void(op_id, self._clock.now())
            return
        if pending.op.state == INSTALL_APPROVED:
            await self._settle_approved(pending, client)
            return

        await self._settle_issued(pending, client)

    async def _settle_approved(self, pending: PendingInstall, client: ShikiruClient) -> None:
        view = await client.get_escalation(pending.op.payload.escalation_id)
        if view is None:
            return
        if view.escalation.status != EscalationStatus.OPEN:
            self._log.void_approved(pending.op.id, self._clock.now())
            return
        self._runner.start(pending.op.id)

    async def _settle_issued(self, pending: PendingInstall, client: ShikiruClient) -> None:
        view = await client.get_escalation(pending.op.payload.escalation_id)
        if view is None:
            return
        now = self._clock.now()
        if view.escalation.status != EscalationStatus.OPEN:
            self._closed(pending, view, now)
        elif pending.ack is None:
            await self._decide_by_check(pending, now)
        elif await self._is_lost(pending):
            await self._reshower.reshow(
                pending.op.payload.number,
                pending.signal,
                AGAIN_NOTE,
                key=f"reshow:{pending.op.id}",
            )
            self._log.fail(pending.op.id, now)

    async def _is_lost(self, pending: PendingInstall) -> bool:
        ack = pending.ack
        if ack is None or ack.status != AckStatus.QUEUED:
            return False
        project = pending.op.project
        return await is_lost(
            self._lifecycles,
            self._clients[project],
            project,
            since=pending.since,
            now=self._clock.now(),
            lost_after=self._lost_after,
        )

    def _closed(self, pending: PendingInstall, view: EscalationView, now: datetime) -> None:
        if view.escalation.resolution != ResolutionKind.ANSWER:
            self._log.void(pending.op.id, now)
            return
        if pending.ack is None:
            self._log.void(pending.op.id, now)
            return
        command_id = str(pending.ack.id)
        if pending.resolved_command_id == command_id:
            self._log.confirm(pending.op.id, final_line(pending.op), now)
        elif pending.resolved_command_id is not None:
            self._log.void(pending.op.id, now)

    async def _decide_by_check(self, pending: PendingInstall, now: datetime) -> None:
        operation = pending.op
        try:
            found = await self._check.find(operation.payload.tool)
        except CommandError, ToolNameError:
            self._log.check_failed(operation.id, now)
            raise
        if found is None:
            self._log.check_failed(operation.id, now)
            self._reasker.ask_again(operation.id, operation.payload, pending.signal)
            return
        try:
            ack = await send_answer(self._clients[operation.project], operation, found)
        except ShikiruRejectedError as error:
            reason = str(error)
            if self._log.record_rejection(operation.id, reason, now):
                await self._deliver_rejection(pending, reason)
            return
        self._log.acknowledge(operation.id, ack, final_line(operation), now)

    async def _deliver_rejection(self, pending: PendingInstall, reason: str) -> None:
        shown = await self._reshower.reshow(
            pending.op.payload.number,
            pending.signal,
            f"Shikiru rejected the install answer: {reason}",
            key=f"install-rejected:{pending.op.id}",
            even_if_closed=True,
        )
        if shown:
            self._log.void(pending.op.id, self._clock.now())
