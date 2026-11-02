"""The approval stage of installing a missing host tool (design §6.3)."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.callback import decline_data, install_data
from shikiri_yaku.domain.outbound import INSTALL_SLOT, Purpose
from shikiri_yaku.domain.outcome import Forward, Notified
from shikiri_yaku.domain.recipes import InstallOpPayload
from shikiri_yaku.extensions.infrastructure.rules import DECISION_REQUIRED
from shikiri_yaku.ports import Button, Message

if TYPE_CHECKING:
    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import (
        Clock,
        Destinations,
        InstallLog,
        InstallPreparer,
        InstallProposalLog,
        Outbox,
        SignalNumbers,
    )

REASONS: Final = frozenset({DECISION_REQUIRED})
OP_IDENTITY: Final = "install:"
INSTALL_INTENDED: Final = "intended"
EFFECT: Final = (
    "adds {tool} for every component on this host; the package manager may also update "
    "dependencies of that tool"
)


class InstallAction:
    """Journal one validated install op before queuing its stored approval."""

    reasons = REASONS

    def __init__(
        self,
        *,
        preparation: InstallPreparer,
        proposals: InstallProposalLog,
        numbers: SignalNumbers,
        installs: InstallLog,
        outbox: Outbox,
        clock: Clock,
        destinations: Destinations,
    ) -> None:
        self._preparation = preparation
        self._proposals = proposals
        self._numbers = numbers
        self._installs = installs
        self._outbox = outbox
        self._clock = clock
        self._destinations = destinations

    async def run(self, signal: Signal) -> Outcome:
        operation = self._installs.get_install(f"{OP_IDENTITY}{signal.id}")
        if operation is None:
            if signal.escalation_id is None:
                return Forward("decision_required signal has no escalation")
            number = self._numbers.number_of(signal)
            if number is None:
                return Forward("decision_required signal is not in the journal")
            plan = await self._preparation.prepare(signal)
            if isinstance(plan, Forward):
                return plan
            proposal = self._proposals.install_proposal(signal.id)
            if proposal is None or not proposal.why:
                return Forward("host-tool classification has no complete stored proposal")
            operation = self._installs.intend_install(
                f"{OP_IDENTITY}{signal.id}",
                signal,
                InstallOpPayload(
                    number=number,
                    escalation_id=signal.escalation_id,
                    tool=plan.tool,
                    manager=plan.manager,
                    package=plan.package,
                    why=proposal.why,
                    argv=plan.argv,
                ),
                self._clock.now(),
            )
        if operation.state != INSTALL_INTENDED:
            return Notified("install approval has already been handled")
        message = _approval(operation.id, operation.payload)
        self._outbox.queue(
            operation.payload.number,
            Purpose.APPROVAL,
            self._destinations.for_signal(signal.project, signal.kind),
            message,
            key=f"{OP_IDENTITY}{operation.id}",
            now=self._clock.now(),
            slot=INSTALL_SLOT,
        )
        return Notified("queued the install approval for the Product Owner")


class InstallApprovalPresenter:
    """Render an approval from an install op's already stored argument list."""

    def render(self, op_id: int, payload: InstallOpPayload) -> Message:
        """Return the approval message whose buttons name this persisted operation."""
        return _approval(op_id, payload)


def _approval(op_id: int, payload: InstallOpPayload) -> Message:
    text = "\n\n".join(
        (
            f"Tool: {payload.tool}",
            f"Why (model-written): {payload.why}",
            f"Command: {shlex.join(payload.argv)}",
            f"Package manager: {payload.manager}",
            EFFECT.format(tool=payload.tool),
        )
    )
    return Message(
        text,
        (
            Button("Install", install_data(payload.number, op_id)),
            Button("Decline", decline_data(payload.number, op_id)),
        ),
        heading=f"S{payload.number} · Install approval",
    )
