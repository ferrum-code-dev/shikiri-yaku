"""Answers the PO's plain text: commands get their reply, anything else the list."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.app.command_reply import item_destination, queue_command_reply
from shikiri_yaku.domain.outbound import fit

if TYPE_CHECKING:
    from shikiri_yaku.app.expert import Ask
    from shikiri_yaku.app.goal import GoalCommand
    from shikiri_yaku.app.open_command import OpenCommand
    from shikiri_yaku.app.status import Status
    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.ports import Clock, Destinations, Outbox

_STATUS: Final = "/status"
_PENDING: Final = "/pending"
_SUBMIT: Final = "/submit"
_EXPERT: Final = "/expert"
_ADMIN: Final = "/admin"
_HELP_NAME: Final = "/help"
_HELP: Final = (
    f"{_STATUS} · each component's engine, tasks and quota blocks; in a component topic, its own\n"
    f"{_PENDING} · pending escalations and approvals, shown again; in a component topic, its own\n"
    f"{_SUBMIT} <component> <request> · submit a request to a component's Shikiru\n"
    f"{_EXPERT} <question> · open an Expert conversation\n"
    f"{_ADMIN} <question> · open an admin conversation, for listed admins\n"
    f"{_HELP_NAME} · this list"
)
_UNKNOWN: Final = "Unknown command {name}.\n{help}"
_KEY_PREFIX: Final = "command:"


class Commands:
    """Answers `/status` and `/help`, passes `/pending` and `/submit` on.

    Free text gets the command list. A message that starts with `/` and names no known command gets
    the name as typed, cut to `name_max_chars`, and the list. Names match without case (a phone
    keyboard capitalises the first letter) and without the `@bot` of the channel's addressed form.
    In a thread that names a component, `/status` and `/pending` cover that component only.

    A command reply has no signal, so it is journaled with none. Its notice keys are the update id
    and the part's index: a repeated delivery of the same update queues nothing.

    With `shared_group`, an Expert topic is routed to the Expert before this class; a message
    in a thread that is neither General nor one of this instance's open component topics nor
    its Expert topics belongs to another instance, so it gets no reply and is not journaled.
    Free text outside such a thread gets the list, as anywhere else.
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
        shared_group: bool = False,
        ask: Ask | None = None,
    ) -> None:
        self._outbox = outbox
        self._clock = clock
        self._status = status
        self._open = open_command
        self._goal = goal
        self._destinations = destinations
        self._channel = channel
        self._name_max_chars = name_max_chars
        self._shared_group = shared_group
        self._ask = ask

    async def handle(self, item: Inbound) -> None:
        word = _first_word(item.text)
        name = _command_name(word)
        component = self._destinations.component_of(item.thread)
        in_other_topic = self._shared_group and item.thread is not None and component is None
        if in_other_topic:
            return
        if name in (_EXPERT, _ADMIN) and self._ask is not None:
            if not (self._shared_group and name == _ADMIN and not self._ask.is_admin(item.sender)):
                if (
                    self._shared_group
                    and item.thread is None
                    and not await self._ask.addressed(word)
                ):
                    if word.lower() in (_EXPERT, _ADMIN):
                        await self._ask.offer(item)
                else:
                    await self._ask.handle(item, component)
            return
        if name == _PENDING:
            await self._open.handle(item, component)
            return
        if name == _SUBMIT:
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

    async def tap(self, item: Inbound) -> bool:
        return self._ask is not None and await self._ask.tap(item)

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
