"""The infrastructure extension: the category whose actions repair a fault (design §6.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.extension import Extension
from shikiri_yaku.extensions.infrastructure.engine_error import EngineErrorAction
from shikiri_yaku.extensions.infrastructure.handler import InfrastructureHandler
from shikiri_yaku.extensions.infrastructure.restart import RestartAction
from shikiri_yaku.extensions.infrastructure.retry import RetryAction
from shikiri_yaku.extensions.infrastructure.rules import (
    HOST_TOOL_RULE,
    INFRASTRUCTURE,
    INFRASTRUCTURE_RULE,
    stands_on_failure,
)
from shikiri_yaku.extensions.infrastructure.wait import WaitAction

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import timedelta

    from shikiri_yaku.domain.extension import Action
    from shikiri_yaku.ports import (
        Clock,
        Guard,
        Notifier,
        RetryLog,
        ShikiruClient,
        StartLog,
        WaitLog,
    )


def infrastructure_extension(
    clients: Mapping[str, ShikiruClient],
    guard: Guard,
    retries: RetryLog,
    clock: Clock,
    *,
    waits: WaitLog,
    starts: StartLog,
    notifier: Notifier,
    install: Action,
    quota_wait_max: timedelta,
    quota_wait_margin: timedelta,
    restart_confirm: timedelta,
    restart_poll: timedelta,
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
    restart = RestartAction(
        clients,
        guard,
        starts,
        notifier=notifier,
        clock=clock,
        confirm_within=restart_confirm,
        poll=restart_poll,
    )
    engine_error = EngineErrorAction(clients, restart, retry, starts)
    actions = (retry, wait, restart, engine_error, install)
    return Extension(
        INFRASTRUCTURE,
        (INFRASTRUCTURE_RULE, HOST_TOOL_RULE),
        InfrastructureHandler(actions),
        actions,
        stands_on_failure=stands_on_failure,
    )
