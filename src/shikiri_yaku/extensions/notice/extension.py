"""The notice extension: the category of information the PO does not decide on (design §5, §6.5)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.extension import Extension
from shikiri_yaku.extensions.notice.handler import NoticeHandler
from shikiri_yaku.extensions.notice.rules import (
    LEGACY_COPIES,
    NOTICE,
    NOTICE_RULE,
    stands_on_failure,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.ports import (
        Clock,
        Destinations,
        Notifier,
        Outbox,
        ShikiruClient,
        SignalNumbers,
    )


def notice_extension(
    clients: Mapping[str, ShikiruClient],
    numbers: SignalNumbers,
    outbox: Outbox,
    notifier: Notifier,
    clock: Clock,
    *,
    destinations: Destinations,
) -> Extension:
    """Return the notice category; it is registered before the product catch-all."""
    handler = NoticeHandler(clients, numbers, outbox, notifier, clock, destinations=destinations)
    return Extension(
        NOTICE,
        (NOTICE_RULE,),
        handler,
        stands_on_failure=stands_on_failure,
        legacy_copies=LEGACY_COPIES,
    )
