"""/open: shows open escalations and pending approvals again, with their buttons (design §7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.app.command_reply import queue_command_reply
from shikiri_yaku.app.replies import answer_data, confirm_key, confirm_message
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.ports import (
    EscalationResolution,
    EscalationStatus,
    ShikiruError,
    escalation_choices,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.ports import (
        Clock,
        EscalationView,
        Message,
        OpenLog,
        Outbox,
        Reshower,
        ShikiruClient,
    )


class OpenCommand:
    """Re-shows what the PO can still answer, to the owners and the sender's chat.

    A question is shown again by the Reshower from Shikiru's current view. An approval is rebuilt
    from that view too, and each copy's key names the op and the update, so the Confirm of every
    copy approves the same op. Nothing is created, approved or issued here: a tap does that.
    """

    def __init__(
        self,
        log: OpenLog,
        clients: Mapping[str, ShikiruClient],
        reshower: Reshower,
        outbox: Outbox,
        clock: Clock,
        *,
        destinations: Sequence[Destination],
        channel: str,
    ) -> None:
        self._log = log
        self._clients = clients
        self._reshower = reshower
        self._outbox = outbox
        self._clock = clock
        self._destinations = tuple(destinations)
        self._channel = channel

    async def handle(self, item: Inbound) -> None:
        listed, unreachable = await self._listed()
        for number, signal in self._log.forwarded():
            if (signal.project, signal.escalation_id) in listed:
                await self._reshower.reshow(
                    number, signal, "", key=f"open:{item.update_id}:{number}"
                )
        for approval in self._log.pending_approvals():
            view = listed.get((approval.project, approval.escalation_id))
            option = None if view is None else _option(view, approval.kind)
            if option is None:
                continue
            self._queue(
                approval.number,
                Purpose.APPROVAL,
                confirm_message(
                    approval.number, approval.kind, answer_data(approval.number, option)
                ),
                key=confirm_key(approval.op_id, item.update_id),
            )
        if unreachable:
            queue_command_reply(
                self._outbox,
                [Destination(self._channel, str(item.sender))],
                f"Shikiru did not answer for {', '.join(unreachable)}.",
                key=f"open:{item.update_id}:unreachable",
                now=self._clock.now(),
            )

    async def _listed(self) -> tuple[dict[tuple[str, str], EscalationView], list[str]]:
        """Return the open escalations by project and id, and the projects that did not answer."""
        listed: dict[tuple[str, str], EscalationView] = {}
        unreachable: list[str] = []
        for project, client in self._clients.items():
            try:
                views = await client.list_escalations()
            except ShikiruError:
                unreachable.append(project)
                continue
            for view in views:
                if view.escalation.status == EscalationStatus.OPEN:
                    listed[(project, view.escalation.id)] = view
        return listed, unreachable

    def _queue(self, number: int, purpose: Purpose, message: Message, *, key: str) -> None:
        self._outbox.queue(
            number, purpose, self._destinations, message, key=key, now=self._clock.now()
        )


def _option(view: EscalationView, kind: str) -> int | None:
    """Return the number of the resolution of the kind in the view, or None if it is not offered."""
    return next(
        (
            choice.number
            for choice in escalation_choices(view)
            if isinstance(choice.value, EscalationResolution) and choice.value.kind == kind
        ),
        None,
    )
