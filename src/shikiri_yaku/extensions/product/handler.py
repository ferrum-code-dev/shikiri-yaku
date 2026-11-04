"""The Product handler: forwards a signal only the PO can decide."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.outcome import Forward

if TYPE_CHECKING:
    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.domain.signal import Signal

NOTHING_TRIED: Final = "nothing, this is a product question"


class ProductHandler:
    """Forwards every signal it is given. The message is built by the forwarder, not here.

    Its note says that nothing was tried, because a product question has nothing for the Operator
    to try. The Router hands the signal to the product forwarder with that note.
    """

    async def handle(self, signal: Signal) -> Outcome:
        del signal
        return Forward(NOTHING_TRIED)
