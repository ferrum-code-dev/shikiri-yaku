"""The product fallback: puts a forwarded signal in front of the owners of the product (§6.1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.outbound import QUESTION_SLOT, Purpose, fit
from shikiri_yaku.extensions.product.message import escalation_message, information_message
from shikiri_yaku.ports import EscalationStatus, ShikiruError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import (
        Clock,
        Destinations,
        EscalationView,
        Message,
        Outbox,
        ShikiruClient,
    )


class ProductForwarder:
    """The Router's Forwarder: queues the message of a forwarded signal for each destination.

    A signal with an escalation is shown as its structured view. A signal without one, or whose
    escalation Shikiru no longer returns, is shown as information. The task's title is optional:
    when Shikiru cannot give it, the header names the task alone. A failure to read the view or to
    queue propagates, so the Router retries the forward later.
    """

    def __init__(
        self,
        title_max_chars: int,
        clients: Mapping[str, ShikiruClient],
        outbox: Outbox,
        clock: Clock,
        destinations: Destinations,
        *,
        button_label_max_chars: int,
    ) -> None:
        self._title_max_chars = title_max_chars
        self._button_label_max_chars = button_label_max_chars
        self._clients = clients
        self._outbox = outbox
        self._clock = clock
        self._destinations = destinations

    async def forward(self, number: int, signal: Signal, note: str, suggestion: str) -> None:
        view = await self._view(signal)
        title = await self._title(signal)
        self._show(
            number,
            signal,
            view,
            title=title,
            note=note,
            suggestion=suggestion,
            key=f"forward:{number}",
        )

    async def reshow(
        self,
        number: int,
        signal: Signal,
        note: str,
        *,
        key: str,
        even_if_closed: bool = False,
    ) -> bool:
        """Queue the signal's question again as a new message, once per key, if still open.

        The view that is checked is the one rendered. A missing or closed view queues nothing,
        unless `even_if_closed` requests an informational copy with the supplied note. Returns
        whether an outbox message was queued.
        """
        view = await self._view(signal)
        if not even_if_closed and (view is None or view.escalation.status != EscalationStatus.OPEN):
            return False
        title = await self._title(signal)
        visible_view = view
        if view is not None and view.escalation.status != EscalationStatus.OPEN:
            visible_view = None
        self._show(number, signal, visible_view, title=title, note=note, suggestion="", key=key)
        return True

    def _show(
        self,
        number: int,
        signal: Signal,
        view: EscalationView | None,
        *,
        title: str,
        note: str,
        suggestion: str,
        key: str,
    ) -> None:
        now = self._clock.now()
        # keep: a handler may run twice for one signal, and the key makes the second queue nothing
        for destination in self._destinations.for_signal(signal.project, signal.kind):
            in_topic = self._destinations.component_of(destination.thread) == signal.project
            purpose, message = self._render(
                number, signal, view, title, in_topic=in_topic, note=note, suggestion=suggestion
            )
            # keep: only an open question has a slot; an information copy never replaces a question
            slot = QUESTION_SLOT if purpose == Purpose.QUESTION else None
            self._outbox.queue(number, purpose, [destination], message, key=key, now=now, slot=slot)

    def _render(
        self,
        number: int,
        signal: Signal,
        view: EscalationView | None,
        title: str,
        *,
        in_topic: bool,
        note: str,
        suggestion: str,
    ) -> tuple[Purpose, Message]:
        if view is None:
            information = information_message(
                number,
                signal,
                title=title,
                in_topic=in_topic,
                note=note,
                suggestion=suggestion,
            )
            return Purpose.NOTICE, information
        question = escalation_message(
            number,
            signal,
            view,
            title=title,
            in_topic=in_topic,
            note=note,
            suggestion=suggestion,
            label_max_chars=self._button_label_max_chars,
        )
        return Purpose.QUESTION, question

    async def _view(self, signal: Signal) -> EscalationView | None:
        if signal.escalation_id is None:
            return None
        client = self._clients.get(signal.project)
        if client is None:
            return None
        return await client.get_escalation(signal.escalation_id)

    async def _title(self, signal: Signal) -> str:
        if signal.task_id is None:
            return ""
        client = self._clients.get(signal.project)
        if client is None:
            return ""
        try:
            detail = await client.get_task(signal.task_id)
        except ShikiruError:
            return ""
        return "" if detail is None else fit(detail.task.title, self._title_max_chars)
