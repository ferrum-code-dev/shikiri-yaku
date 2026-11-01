"""Settles the installs a restart found unsettled (design §6.3 step 7, §10), once per start.

An approved op no run claimed is run. An issued op is decided by its escalation, then by its check,
and is never repeated: a tool found is answered, a tool still missing is asked again, and an answer
the lost link did not deliver is shown to the PO again. An op a run of this process has in hand is
left alone, and each op is read again before its decision, so an earlier list cannot act twice.
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
    """Runs once at start: starts approved installs and settles the issued ones by observation."""

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

    async def run(self) -> None:
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
        if pending.op.state == INSTALL_APPROVED:
            self._runner.start(op_id)
            return
        project = pending.op.project
        view = await self._clients[project].get_escalation(pending.op.payload.escalation_id)
        if view is None:
            return
        now = self._clock.now()
        if view.escalation.status != EscalationStatus.OPEN:
            self._closed(pending, view, now)
        elif pending.ack is None:
            await self._decide_by_check(pending, now)
        elif await self._is_lost(pending):
            await self._reshower.reshow(
                pending.op.payload.number, pending.signal, AGAIN_NOTE, key=f"reshow:{op_id}"
            )
            self._log.fail(op_id, now)

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
        # keep: an answer that closed the escalation confirms the op; another close voids it
        if view.escalation.resolution == ResolutionKind.ANSWER:
            self._log.confirm(pending.op.id, final_line(pending.op), now)
        else:
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
        except ShikiruRejectedError:
            # keep: the escalation stays forwarded, so the PO still has the question
            self._log.void(operation.id, now)
            return
        self._log.acknowledge(operation.id, ack, final_line(operation), now)
