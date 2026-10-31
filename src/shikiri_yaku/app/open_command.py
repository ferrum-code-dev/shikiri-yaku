"""/open: shows open escalations and pending approvals again, with their buttons (design §7)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.app.command_reply import item_destination, queue_command_reply
from shikiri_yaku.app.replies import answer_data, confirm_key, confirm_message
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.signal import Kind
from shikiri_yaku.ports import (
    EscalationResolution,
    EscalationStatus,
    ShikiruError,
    escalation_choices,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.ports import (
        Clock,
        Destinations,
        EscalationView,
        Message,
        OpenLog,
        Outbox,
        Reshower,
        ShikiruClient,
    )

_NOTHING: Final = "Nothing is waiting for you."


class OpenCommand:
    """Re-shows what the PO can still answer, to the owners and the sender's chat.

    A question is shown again by the Reshower from Shikiru's current view. An approval is rebuilt
    from that view too, and each copy's key names the op and the update, so the Confirm of every
    copy approves the same op. Nothing is created, approved or issued here: a tap does that. When
    nothing was shown and every project answered, the sender is told that nothing is waiting.
    """

    def __init__(
        self,
        log: OpenLog,
        clients: Mapping[str, ShikiruClient],
        reshower: Reshower,
        outbox: Outbox,
        clock: Clock,
        *,
        destinations: Destinations,
        channel: str,
    ) -> None:
        self._log = log
        self._clients = clients
        self._reshower = reshower
        self._outbox = outbox
        self._clock = clock
        self._destinations = destinations
        self._channel = channel

    async def handle(self, item: Inbound) -> None:
        unreachable_key = f"open:{item.update_id}:unreachable"
        if self._outbox.command_reply_queued(unreachable_key):
            return
        listed, unreachable = await self._listed()
        shown = await self._reshow_questions(item.update_id, listed)
        shown += self._queue_approvals(item.update_id, listed)
        if unreachable:
            self._reply(
                item, f"Shikiru did not answer for {', '.join(unreachable)}.", unreachable_key
            )
        elif shown == 0:
            self._reply(item, _NOTHING, f"open:{item.update_id}:nothing")

    async def _reshow_questions(
        self, update_id: int, listed: Mapping[tuple[str, str], EscalationView]
    ) -> int:
        """Re-show each forwarded question that Shikiru still lists; return how many were queued."""
        shown = 0
        for number, signal in self._log.forwarded():
            if (signal.project, signal.escalation_id) not in listed:
                continue
            if await self._reshower.reshow(number, signal, "", key=f"open:{update_id}:{number}"):
                shown += 1
        return shown

    def _queue_approvals(
        self, update_id: int, listed: Mapping[tuple[str, str], EscalationView]
    ) -> int:
        """Queue each approval that Shikiru still offers; return how many were queued."""
        queued = 0
        for approval in self._log.pending_approvals():
            view = listed.get((approval.project, approval.escalation_id))
            option = None if view is None else _option(view, approval.kind)
            if option is None:
                continue
            self._queue(
                approval.number,
                approval.project,
                Purpose.APPROVAL,
                confirm_message(
                    approval.number, approval.kind, answer_data(approval.number, option)
                ),
                key=confirm_key(approval.op_id, update_id),
            )
            queued += 1
        return queued

    def _reply(self, item: Inbound, text: str, key: str) -> None:
        queue_command_reply(
            self._outbox,
            [item_destination(self._channel, item)],
            text,
            key=key,
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

    def _queue(
        self,
        number: int,
        project: str,
        purpose: Purpose,
        message: Message,
        *,
        key: str,
    ) -> None:
        self._outbox.queue(
            number,
            purpose,
            self._destinations.for_signal(project, Kind.ESCALATION),
            message,
            key=key,
            now=self._clock.now(),
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
