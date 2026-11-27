"""/pending: shows pending escalations and approvals again, with their buttons."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Final

from shikiri_yaku.app.command_reply import item_destination, queue_command_reply
from shikiri_yaku.app.replies import answer_data, confirm_key, confirm_message
from shikiri_yaku.domain.outbound import INSTALL_SLOT, Purpose
from shikiri_yaku.domain.signal import Kind
from shikiri_yaku.ports import (
    INSTALL_INTENDED,
    EscalationResolution,
    EscalationStatus,
    ShikiruError,
    escalation_choices,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.ports import (
        Clock,
        Destinations,
        EscalationView,
        InstallApprovalRenderer,
        InstallOp,
        Message,
        OpenLog,
        Outbox,
        Reshower,
        ShikiruClient,
    )

_NOTHING: Final = "Nothing is waiting for you."
_NOTHING_IN: Final = "Nothing is waiting for you in {component}."


def _done_key(update_id: int) -> str:
    return f"pending:{update_id}:done"


def _nothing(component: str | None) -> str:
    return _NOTHING if component is None else _NOTHING_IN.format(component=component)


class OpenCommand:
    """Re-shows what the PO can still answer, to the owners and the sender's chat.

    A question is shown again by the Reshower from Shikiru's current view. An approval is rebuilt
    from that view too, and each copy's key names the op and the update, so the Confirm of every
    copy approves the same op. Nothing is created, approved or issued here: a tap does that. When
    nothing was shown and every project answered, the sender is told that nothing is waiting.

    The reply groups the shown items under Questions and Approvals headings.

    A command sent in a thread that names a component asks only that component's Shikiru, so only
    its items are shown and only its name is reported.

    An update is answered once. Its completion is journaled after every part, so a repeat of the
    update queues nothing even if Shikiru has changed since. An answer interrupted before that is
    finished by its replay, and each part's own key keeps the replay from sending it twice.
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
        max_text: int,
        install_approval: InstallApprovalRenderer | None = None,
        approval_expiry_h: int | None = None,
    ) -> None:
        self._log = log
        self._clients = clients
        self._reshower = reshower
        self._outbox = outbox
        self._clock = clock
        self._destinations = destinations
        self._channel = channel
        self._max_text = max_text
        self._install_approval = install_approval
        self._approval_expiry_h = approval_expiry_h

    async def handle(self, item: Inbound, component: str | None) -> None:
        if self._outbox.command_reply_queued(_done_key(item.update_id)):
            return
        listed, unreachable = await self._listed(component)
        questions = await self._reshow_questions(item.update_id, listed)
        approvals = self._queue_approvals(item.update_id, listed)
        installs = self._queue_install_approvals(item.update_id, listed)
        if questions or approvals or installs:
            self._reply(
                item,
                _grouped(questions, approvals, installs),
                f"pending:{item.update_id}:summary",
            )
        elif not unreachable:
            self._reply(item, _nothing(component), f"pending:{item.update_id}:nothing")
        if unreachable:
            self._reply(
                item,
                f"Shikiru did not answer for {', '.join(unreachable)}.",
                f"pending:{item.update_id}:unreachable",
            )
        self._complete(item)

    async def _reshow_questions(
        self, update_id: int, listed: Mapping[tuple[str, str], EscalationView]
    ) -> list[int]:
        shown: list[int] = []
        for number, signal in self._log.forwarded():
            if (signal.project, signal.escalation_id) not in listed:
                continue
            if await self._reshower.reshow(number, signal, "", key=f"pending:{update_id}:{number}"):
                shown.append(number)
        return shown

    def _queue_approvals(
        self, update_id: int, listed: Mapping[tuple[str, str], EscalationView]
    ) -> list[tuple[int, str]]:
        shown: list[tuple[int, str]] = []
        for approval in self._log.pending_approvals():
            view = listed.get((approval.project, approval.escalation_id))
            option = None if view is None else approval_option(view, approval.kind)
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
                slot=confirm_key(approval.op_id),
            )
            shown.append((approval.number, approval.kind))
        return shown

    def _queue_install_approvals(
        self, update_id: int, listed: Mapping[tuple[str, str], EscalationView]
    ) -> list[int]:
        if self._install_approval is None or self._approval_expiry_h is None:
            return []
        shown: list[int] = []
        now = self._clock.now()
        for operation in self._log.pending_install_approvals():
            payload = operation.payload
            if (operation.project, payload.escalation_id) not in listed or not install_is_pending(
                operation, now, self._approval_expiry_h
            ):
                continue
            self._queue(
                payload.number,
                operation.project,
                Purpose.APPROVAL,
                self._install_approval.render(operation.id, payload),
                key=f"install:{operation.id}:pending:{update_id}",
                slot=INSTALL_SLOT,
            )
            shown.append(payload.number)
        return shown

    def _reply(self, item: Inbound, text: str, key: str) -> None:
        queue_command_reply(
            self._outbox,
            [item_destination(self._channel, item)],
            text,
            key=key,
            now=self._clock.now(),
            max_text=self._max_text,
        )

    def _complete(self, item: Inbound) -> None:
        self._outbox.queue_command_reply(
            [], [], key=_done_key(item.update_id), now=self._clock.now()
        )

    async def _listed(
        self, component: str | None
    ) -> tuple[dict[tuple[str, str], EscalationView], list[str]]:
        listed: dict[tuple[str, str], EscalationView] = {}
        unreachable: list[str] = []
        for project, client in self._clients.items():
            if component is not None and project != component:
                continue
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
        slot: str,
    ) -> None:
        self._outbox.queue(
            number,
            purpose,
            self._destinations.for_signal(project, Kind.ESCALATION),
            message,
            key=key,
            now=self._clock.now(),
            slot=slot,
        )


def _grouped(questions: list[int], approvals: list[tuple[int, str]], installs: list[int]) -> str:
    parts: list[str] = []
    if questions:
        parts.append("Questions:\n" + "\n".join(f"S{n}" for n in questions))
    approval_lines = [f"S{n} · {kind}" for n, kind in approvals]
    approval_lines.extend(f"S{n} · install" for n in installs)
    if approval_lines:
        parts.append("Approvals:\n" + "\n".join(approval_lines))
    return "\n\n".join(parts)


def approval_option(view: EscalationView, kind: str) -> int | None:
    """Return the current option for a pending approval, if it can be shown."""
    return next(
        (
            choice.number
            for choice in escalation_choices(view)
            if isinstance(choice.value, EscalationResolution) and choice.value.kind == kind
        ),
        None,
    )


def install_is_pending(operation: InstallOp, now: datetime, expiry_h: int) -> bool:
    """Return whether an install approval is still eligible to be shown."""
    return operation.state == INSTALL_INTENDED and now - operation.created_at < timedelta(
        hours=expiry_h
    )
