"""Answers `/submit <component> <request>`: submits the request and reports the outcome."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from shikiri_yaku.app.command_reply import queue_command_reply
from shikiri_yaku.domain.callback import SUBMIT_VERB, decode_callback, submit_data
from shikiri_yaku.domain.outbound import Purpose, fit
from shikiri_yaku.ports import (
    Button,
    Message,
    SubmitPayload,
    SubmitStatus,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.app.submit import Submitter
    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.ports import Clock, Outbox, Places, ReplyLog, ShikiruClient, SubmitOp

_WORDS: Final = 3
_IMPLIED_WORDS: Final = 2
_ASK_HEAD: Final = "I may have submitted this already: "
_ASK_TAIL: Final = ". Check /status, or tap Submit again."
_SUBMIT_AGAIN: Final = "Submit again"


class GoalCommand:
    """Submits a request and reports its outcome in the place it was sent from.

    The place is stored with the request, so the outcome and any question go
    where the request was sent. A request sent in a component's place
    implies that component. An unknown outcome is asked about with *Submit again*;
    only that tap retries the request.

    With `shared_chat`, a request that names a component this instance does not
    have belongs to another instance, so it gets no reply.
    """

    def __init__(
        self,
        submitter: Submitter,
        log: ReplyLog,
        clients: Mapping[str, ShikiruClient],
        outbox: Outbox,
        clock: Clock,
        *,
        places: Places,
        max_text: int,
        shared_chat: bool = False,
    ) -> None:
        self._submitter = submitter
        self._log = log
        self._clients = clients
        self._outbox = outbox
        self._clock = clock
        self._places = places
        self._max_text = max_text
        self._shared_chat = shared_chat

    async def handle(self, item: Inbound) -> None:
        component, request = self._parse(item)
        if self._shared_chat and component is not None and component not in self._clients:
            return
        if component is None or request is None or component not in self._clients:
            names = ", ".join(sorted(self._clients))
            queue_command_reply(
                self._outbox,
                [item.place],
                f"Usage: /submit <component> <request>. Components: {names}.",
                key=f"submit:usage:{item.id}",
                now=self._clock.now(),
                max_text=self._max_text,
            )
            return
        payload = SubmitPayload(component=component, request=request, place=item.place)
        self._report(await self._submitter.submit(item.id, payload))

    async def tap(self, item: Inbound) -> None:
        """Retry the request of the *Submit again* button on the tapped message."""
        message = self._log.sent_message(item.place.channel, item.message_ref)
        if message is None or not item.data:
            return
        if item.data not in {button.data for button in message.buttons}:
            return
        try:
            callback = decode_callback(item.data)
        except ValidationError:
            return
        if callback[0] != SUBMIT_VERB:
            return
        op = await self._submitter.retry(callback[1], item.id)
        if op is not None:
            self._report(op)

    def recover(self) -> None:
        """Ask about each request an earlier process left unknown."""
        for op in self._submitter.recover():
            self._report(op)

    def _parse(self, item: Inbound) -> tuple[str | None, str | None]:
        """Return the component and request of a submit; a part the text lacks is None.

        In a component's place the whole text after `/submit` is the request; elsewhere
        the text is `/submit <component> <request>`.
        """
        implied = self._places.component_of(item.place)
        if implied is not None:
            words = (item.text or "").split(maxsplit=1)
            return implied, words[1] if len(words) == _IMPLIED_WORDS else None
        words = (item.text or "").split(maxsplit=_WORDS - 1)
        component = words[1] if len(words) > 1 else None
        return component, words[_WORDS - 1] if len(words) == _WORDS else None

    def _report(self, op: SubmitOp) -> None:
        places = [op.payload.place]
        key = f"submit:{op.id}"
        now = self._clock.now()
        if op.status == SubmitStatus.CONFIRMED:
            text = f"Request submitted as task {op.task_id}."
            queue_command_reply(
                self._outbox, places, text, key=key, now=now, max_text=self._max_text
            )
        elif op.status == SubmitStatus.FAILED:
            text = "Shikiru did not take this request; it was not submitted."
            queue_command_reply(
                self._outbox, places, text, key=key, now=now, max_text=self._max_text
            )
        elif op.status == SubmitStatus.UNKNOWN:
            room = self._max_text - len(_ASK_HEAD) - len(_ASK_TAIL)
            text = f"{_ASK_HEAD}{fit(op.payload.request, room)}{_ASK_TAIL}"
            button = Button(_SUBMIT_AGAIN, submit_data(op.id))
            self._outbox.queue(
                None,
                Purpose.QUESTION,
                places,
                Message(text, (button,)),
                key=key,
                now=now,
            )
