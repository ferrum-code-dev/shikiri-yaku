"""Asks the PO again for an install whose check found the tool still missing (design §6.3).

The new approval shows the stored command of the old op, so nothing is planned again. It is queued
before the old op is voided: a crash in between leaves the old op issued, and the next start calls
this again, which finds the same new op and queues nothing twice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.ports import INSTALL_INTENDED

if TYPE_CHECKING:
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import (
        Clock,
        Destinations,
        InstallApprovalRenderer,
        InstallOp,
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

    def ask_again(self, old: InstallOp, signal: Signal) -> None:
        """Make the PO's new approval of the same command, then void `old`."""
        now = self._clock.now()
        new = self._installs.intend_install(
            f"install:{signal.id}:after:{old.id}", signal, old.payload, now
        )
        if new.state == INSTALL_INTENDED:
            self._outbox.queue(
                old.payload.number,
                Purpose.APPROVAL,
                self._destinations.for_signal(signal.project, signal.kind),
                self._renderer.render(new.id, new.payload),
                key=f"install:{new.id}",
                now=now,
            )
        self._installs.void_issued(old.id, now)
