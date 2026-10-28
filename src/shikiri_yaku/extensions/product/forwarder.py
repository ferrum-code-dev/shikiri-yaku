"""The product fallback: puts a forwarded signal in front of the owners of the product (§6.1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.extensions.product.message import escalation_message, information_message

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import Channel, EscalationView, ShikiruClient


class ProductForwarder:
    """The Router's Forwarder: sends the message of a forwarded signal to each destination.

    A signal with an escalation is shown as its structured view. A signal without one, or whose
    escalation Shikiru no longer returns, is shown as information. A failure to read the view or to
    send propagates, so the Router retries the forward later.
    """

    def __init__(
        self,
        product: str,
        clients: Mapping[str, ShikiruClient],
        channel: Channel,
        destinations: Sequence[str],
    ) -> None:
        self._product = product
        self._clients = clients
        self._channel = channel
        self._destinations = tuple(destinations)

    async def forward(self, number: int, signal: Signal, note: str, suggestion: str) -> None:
        view = await self._view(signal)
        if view is None:
            message = information_message(
                number, signal, product=self._product, note=note, suggestion=suggestion
            )
        else:
            message = escalation_message(
                number, signal, view, product=self._product, note=note, suggestion=suggestion
            )
        for destination in self._destinations:
            await self._channel.send(destination, message)

    async def _view(self, signal: Signal) -> EscalationView | None:
        if signal.escalation_id is None:
            return None
        return await self._clients[signal.project].get_escalation(signal.escalation_id)
