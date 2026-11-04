"""The infrastructure handler: runs the action that claims a signal's reason or kind."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shikiri_yaku.domain.outcome import Forward
from shikiri_yaku.domain.rule_classifier import RegistrationError
from shikiri_yaku.domain.signal import Kind

if TYPE_CHECKING:
    from collections.abc import Sequence

    from shikiri_yaku.domain.extension import Action
    from shikiri_yaku.domain.outcome import Outcome
    from shikiri_yaku.domain.signal import Signal


class InfrastructureHandler:
    """Runs the one action that claims the signal's reason, or its kind for the other signals.

    A signal no action claims forwards with a note that names it, so nothing is dropped silently.
    Two actions claiming one reason are refused when the handler is built, so a signal never has
    two actions to choose from.
    """

    def __init__(self, actions: Sequence[Action]) -> None:
        self._actions: dict[str, Action] = {}
        for action in actions:
            for name in action.reasons:
                if name in self._actions:
                    message = f"reason '{name}' is claimed by two infrastructure actions"
                    raise RegistrationError(message)
                self._actions[name] = action

    async def handle(self, signal: Signal) -> Outcome:
        name = signal.reason if signal.kind is Kind.ESCALATION else signal.kind.value
        action = self._actions.get(name)
        if action is None:
            return Forward(f"no infrastructure action covers '{name}'")
        return await action.run(signal)
