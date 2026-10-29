"""The infrastructure extension: the category whose actions repair a fault (design §6.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.extension import Extension
from shikiri_yaku.extensions.infrastructure.handler import InfrastructureHandler
from shikiri_yaku.extensions.infrastructure.retry import RetryAction
from shikiri_yaku.extensions.infrastructure.rules import INFRASTRUCTURE, INFRASTRUCTURE_RULE
from shikiri_yaku.extensions.infrastructure.wait import WaitAction

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import timedelta

    from shikiri_yaku.ports import Clock, Guard, Notifier, RetryLog, ShikiruClient, WaitLog


def infrastructure_extension(
    clients: Mapping[str, ShikiruClient],
    guard: Guard,
    retries: RetryLog,
    clock: Clock,
    *,
    waits: WaitLog,
    notifier: Notifier,
    quota_wait_max: timedelta,
    quota_wait_margin: timedelta,
) -> Extension:
    """Return the infrastructure category; it is registered before the product catch-all."""
    retry = RetryAction(clients, guard, retries, clock)
    wait = WaitAction(
        clients,
        retry,
        waits,
        notifier,
        clock,
        max_wait=quota_wait_max,
        margin=quota_wait_margin,
    )
    actions = (retry, wait)
    return Extension(
        INFRASTRUCTURE, (INFRASTRUCTURE_RULE,), InfrastructureHandler(actions), actions
    )
