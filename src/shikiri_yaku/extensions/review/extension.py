"""The review extension: the category of a pull request ready for review."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.extension import Extension
from shikiri_yaku.extensions.review.handler import ReviewHandler
from shikiri_yaku.extensions.review.rules import (
    LEGACY_COPIES,
    REVIEW,
    REVIEW_RULE,
    stands_on_failure,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.ports import Clock, Destinations, Outbox, ShikiruClient, SignalNumbers


def review_extension(
    clients: Mapping[str, ShikiruClient],
    numbers: SignalNumbers,
    outbox: Outbox,
    clock: Clock,
    *,
    product: str,
    destinations: Destinations,
) -> Extension:
    """Return the review category; it is registered before the product catch-all."""
    handler = ReviewHandler(
        clients, numbers, outbox, clock, product=product, destinations=destinations
    )
    return Extension(
        REVIEW,
        (REVIEW_RULE,),
        handler,
        stands_on_failure=stands_on_failure,
        legacy_copies=LEGACY_COPIES,
    )
