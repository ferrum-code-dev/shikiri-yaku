"""Answers the PO's plain text: commands get their reply, anything else the list."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.app.command_reply import item_destination, queue_command_reply
from shikiri_yaku.domain.outbound import fit

if TYPE_CHECKING:
    from shikiri_yaku.app.goal import GoalCommand
    from shikiri_yaku.app.open_command import OpenCommand
    from shikiri_yaku.app.status import Status
    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.ports import Clock, Destinations, Outbox

_STATUS: Final = "/status"
_OPEN: Final = "/open"
_GOAL: Final = "/goal"
_HELP_NAME: Final = "/help"
_HELP: Final = (
    f"{_STATUS} · each component's engine, tasks and quota blocks; in a component topic, its own\n"
    f"{_OPEN} · open escalations and approvals, shown again; in a component topic, its own\n"
    f"{_GOAL} <component> <request> · submit a goal to a component's Shikiru\n"
    f"{_HELP_NAME} · this list"
)
_UNKNOWN: Final = "Unknown command {name}.\n{help}"
_KEY_PREFIX: Final = "command:"


class Commands:
    """Answers `/status` and `/help`, passes `/open` and `/goal` on.

    Free text gets the command list. A message that starts with `/` and names no known command gets
    the name as typed, cut to `name_max_chars`, and the list. Names match without case (a phone
    keyboard capitalises the first letter) and without the `@bot` of the channel's addressed form.
    In a thread that names a component, `/status` and `/open` cover that component only.

    A command reply has no signal, so it is journaled with none. Its notice keys are the update id
    and the part's index: a repeated delivery of the same update queues nothing.
    """

    def __init__(
        self,
        outbox: Outbox,
        clock: Clock,
        status: Status,
        open_command: OpenCommand,
        goal: GoalCommand,
        *,
        destinations: Destinations,
        channel: str,
        name_max_chars: int,
    ) -> None:
        self._outbox = outbox
        self._clock = clock
        self._status = status
        self._open = open_command
        self._goal = goal
        self._destinations = destinations
        self._channel = channel
        self._name_max_chars = name_max_chars

    async def handle(self, item: Inbound) -> None:
        word = _first_word(item.text)
        name = _command_name(word)
        component = self._destinations.component_of(item.thread)
        if name == _OPEN:
            await self._open.handle(item, component)
            return
        if name == _GOAL:
            await self._goal.handle(item)
            return
        key = f"{_KEY_PREFIX}{item.update_id}"
        if self._outbox.command_reply_queued(key):
            return
        queue_command_reply(
            self._outbox,
            [item_destination(self._channel, item)],
            await self._answer(word, name, component),
            key=key,
            now=self._clock.now(),
        )

    async def _answer(self, word: str, name: str, component: str | None) -> str:
        if not word.startswith("/") or name == _HELP_NAME:
            return _HELP
        if name == _STATUS:
            return await self._status.report(component)
        return _UNKNOWN.format(name=fit(word, self._name_max_chars), help=_HELP)


def _first_word(text: str | None) -> str:
    words = (text or "").split(maxsplit=1)
    return words[0] if words else ""


def _command_name(word: str) -> str:
    """Return the command a word names: lower case, without the `@bot` of the addressed form."""
    return word.split("@", 1)[0].lower()
