"""Asks the PO again for an install whose check found the tool still missing.

The new approval shows the stored command of the old op, so nothing is planned again. It is queued
before the old op is voided: a crash in between leaves the old op issued, and the next start calls
this again, which finds the same new op and queues nothing twice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.outbound import INSTALL_SLOT, Purpose
from shikiri_yaku.ports import INSTALL_INTENDED

if TYPE_CHECKING:
    from shikiri_yaku.domain.recipes import InstallOpPayload
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import (
        Clock,
        Destinations,
        InstallApprovalRenderer,
        InstallReaskLog,
        Outbox,
    )


class InstallReasker:
    """Queue a new approval for an issued op's stored command, then void the old op."""

    def __init__(
        self,
        installs: InstallReaskLog,
        renderer: InstallApprovalRenderer,
        outbox: Outbox,
        destinations: Destinations,
        clock: Clock,
    ) -> None:
        self._installs = installs
        self._renderer = renderer
        self._outbox = outbox
        self._destinations = destinations
        self._clock = clock

    def ask_again(self, op_id: int, payload: InstallOpPayload, signal: Signal) -> None:
        """Make the PO's new approval of the stored command of `op_id`, then void that op."""
        now = self._clock.now()
        new = self._installs.intend_install(
            f"install:{signal.id}:after:{op_id}", signal, payload, now
        )
        if new.state == INSTALL_INTENDED:
            self._outbox.queue(
                payload.number,
                Purpose.APPROVAL,
                self._destinations.for_signal(signal.project, signal.kind),
                self._renderer.render(new.id, new.payload),
                key=f"install:{new.id}",
                now=now,
                slot=INSTALL_SLOT,
            )
        self._installs.void_issued(op_id, now)
