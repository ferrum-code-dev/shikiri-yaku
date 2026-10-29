"""The infrastructure extension: the category whose actions repair a fault (design §6.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.extension import Extension
from shikiri_yaku.extensions.infrastructure.handler import InfrastructureHandler
from shikiri_yaku.extensions.infrastructure.retry import RetryAction
from shikiri_yaku.extensions.infrastructure.rules import INFRASTRUCTURE, INFRASTRUCTURE_RULE

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.ports import Guard, ShikiruClient


def infrastructure_extension(clients: Mapping[str, ShikiruClient], guard: Guard) -> Extension:
    """Return the infrastructure category; it is registered before the product catch-all."""
    actions = (RetryAction(clients, guard),)
    return Extension(
        INFRASTRUCTURE, (INFRASTRUCTURE_RULE,), InfrastructureHandler(actions), actions
    )
