"""Answers the PO's plain text: commands get their reply, anything else the list."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from shikiri_yaku.app.command_reply import queue_command_reply
from shikiri_yaku.domain.outbound import fit

if TYPE_CHECKING:
    from shikiri_yaku.app.expert import Ask
    from shikiri_yaku.app.goal import GoalCommand
    from shikiri_yaku.app.open_command import OpenCommand
    from shikiri_yaku.app.status import Status
    from shikiri_yaku.domain.inbound import Command, Inbound
    from shikiri_yaku.ports import Clock, Outbox, Places, SharedChat

_STATUS: Final = "/status"
_DETAILS: Final = "/details"
_PENDING: Final = "/pending"
_SUBMIT: Final = "/submit"
_EXPERT: Final = "/expert"
_ADMIN: Final = "/admin"
_HELP_NAME: Final = "/help"
_HELP: Final = (
    f"{_STATUS} · one line per component; only this component in its chat\n"
    f"{_DETAILS} · every task and quota per component; only this component in its chat\n"
    f"{_PENDING} · open questions and approvals, shown again; only this component in its chat\n"
    f"{_SUBMIT} <component> <request> · submit a request to a component's Shikiru\n"
    f"{_EXPERT} <question> · open an Expert conversation\n"
    f"{_ADMIN} <question> · open an admin conversation, for listed admins\n"
    f"{_HELP_NAME} · this list"
)
_UNKNOWN: Final = "Unknown command {name}.\n{help}"
_KEY_PREFIX: Final = "command:"


class Commands:
    """Answers `/status`, `/details` and `/help`, passes `/pending` and `/submit` on.

    Free text gets the command list. A message that starts with `/` and names no known command gets
    the name as typed, cut to `name_max_chars`, and the list. Names match without case (a phone
    keyboard capitalises the first letter) and without the instance suffix of a shared chat's
    addressed form.
    In a component's place, `/status`, `/details` and `/pending` cover only it.

    A command reply has no signal, so it is journaled with none. Its notice keys are the event id
    and the part's index: a repeated delivery of the same event queues nothing.

    In a shared chat, an open Expert room is routed to the Expert before this class; a message
    in a place that is neither main nor one of this instance's component places belongs to
    another instance, so it gets no reply and is not journaled. Free text elsewhere gets the
    list, as anywhere else.
    """

    def __init__(
        self,
        outbox: Outbox,
        clock: Clock,
        status: Status,
        open_command: OpenCommand,
        goal: GoalCommand,
        *,
        places: Places,
        max_text: int,
        name_max_chars: int,
        shared: SharedChat | None = None,
        ask: Ask | None = None,
    ) -> None:
        self._outbox = outbox
        self._clock = clock
        self._status = status
        self._open = open_command
        self._goal = goal
        self._places = places
        self._max_text = max_text
        self._name_max_chars = name_max_chars
        self._shared = shared
        self._ask = ask

    async def handle(self, item: Inbound) -> None:
        command = item.command
        name = "" if command is None else command.name
        place = item.place
        component = self._places.component_of(place)
        shared = self._shared
        if shared is not None and component is None and not shared.is_main(place):
            return
        if command is not None and name in (_EXPERT, _ADMIN) and self._ask is not None:
            # keep: in a shared chat only an instance that lists the sender answers `/admin`
            if shared is not None and name == _ADMIN and not self._ask.is_admin(item.sender):
                return
            if shared is None or not shared.is_main(place) or await shared.addressed(command.word):
                await self._ask.handle(item, component)
            elif command.instance is None:
                await self._ask.offer(item)
            return
        if name == _PENDING:
            await self._open.handle(item, component)
            return
        if name == _SUBMIT:
            await self._goal.handle(item)
            return
        key = f"{_KEY_PREFIX}{item.id}"
        if self._outbox.command_reply_queued(key):
            return
        queue_command_reply(
            self._outbox,
            [place],
            await self._answer(command, component),
            key=key,
            now=self._clock.now(),
            max_text=self._max_text,
        )

    async def tap(self, item: Inbound) -> bool:
        return self._ask is not None and await self._ask.tap(item)

    async def _answer(self, command: Command | None, component: str | None) -> str:
        if command is None or command.name == _HELP_NAME:
            return _HELP
        if command.name == _STATUS:
            return await self._status.report(component)
        if command.name == _DETAILS:
            return await self._status.details(component)
        return _UNKNOWN.format(name=fit(command.word, self._name_max_chars), help=_HELP)
