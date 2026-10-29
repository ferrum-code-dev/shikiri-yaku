"""The product fallback: puts a forwarded signal in front of the owners of the product (§6.1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.extensions.product.message import escalation_message, information_message
from shikiri_yaku.ports import EscalationStatus

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from shikiri_yaku.domain.schedule import Destination
    from shikiri_yaku.domain.signal import Kind, Signal
    from shikiri_yaku.ports import Clock, EscalationView, Outbox, ShikiruClient


class ProductForwarder:
    """The Router's Forwarder: queues the message of a forwarded signal for each destination.

    A signal with an escalation is shown as its structured view. A signal without one, or whose
    escalation Shikiru no longer returns, is shown as information. A failure to read the view or to
    queue propagates, so the Router retries the forward later.
    """

    def __init__(
        self,
        product: str,
        clients: Mapping[str, ShikiruClient],
        outbox: Outbox,
        clock: Clock,
        destinations: Callable[[str, Kind], Sequence[Destination]],
    ) -> None:
        self._product = product
        self._clients = clients
        self._outbox = outbox
        self._clock = clock
        self._destinations = destinations

    async def forward(self, number: int, signal: Signal, note: str, suggestion: str) -> None:
        view = await self._view(signal)
        self._show(number, signal, view, note, suggestion, key=f"forward:{number}")

    async def reshow(self, number: int, signal: Signal, note: str, *, key: str) -> None:
        """Queue the signal's question again as a new message, once per key, if still open.

        The view that is checked is the one rendered. A missing or closed view queues nothing,
        because the list that named the escalation was read before this fetch.
        """
        view = await self._view(signal)
        if view is None or view.escalation.status != EscalationStatus.OPEN:
            return
        self._show(number, signal, view, note, "", key=key)

    def _show(
        self,
        number: int,
        signal: Signal,
        view: EscalationView | None,
        note: str,
        suggestion: str,
        *,
        key: str,
    ) -> None:
        if view is None:
            purpose = Purpose.NOTICE
            message = information_message(
                number, signal, product=self._product, note=note, suggestion=suggestion
            )
        else:
            purpose = Purpose.QUESTION
            message = escalation_message(
                number, signal, view, product=self._product, note=note, suggestion=suggestion
            )
        # keep: a handler may run twice for one signal, and the key makes the second queue nothing
        self._outbox.queue(
            number,
            purpose,
            self._destinations(signal.project, signal.kind),
            message,
            key=key,
            now=self._clock.now(),
        )

    async def _view(self, signal: Signal) -> EscalationView | None:
        if signal.escalation_id is None:
            return None
        return await self._clients[signal.project].get_escalation(signal.escalation_id)
