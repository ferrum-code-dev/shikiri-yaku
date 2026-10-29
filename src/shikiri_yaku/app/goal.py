"""Answers `/goal <component> <request>`: submits the goal and reports the outcome (§7, §10.1)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from shikiri_yaku.app.command_reply import queue_command_reply
from shikiri_yaku.domain.callback import SUBMIT_VERB, decode_callback, submit_data
from shikiri_yaku.domain.outbound import Purpose, fit
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.ports import (
    MESSAGE_TEXT_LIMIT,
    Button,
    Message,
    SubmitPayload,
    SubmitStatus,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.app.submit import Submitter
    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.ports import Clock, Outbox, ReplyLog, ShikiruClient, SubmitOp

_WORDS: Final = 3
_ASK_HEAD: Final = "I may have submitted this already: "
_ASK_TAIL: Final = ". Check /status, or tap Submit again."
_SUBMIT_AGAIN: Final = "Submit again"


class GoalCommand:
    """Submits a goal and reports its outcome in the chat it was sent from.

    The chat is stored with the goal, so the outcome and any question go where the goal was sent.
    An unknown outcome is asked about with *Submit again*; only that tap retries the goal.
    """

    def __init__(
        self,
        submitter: Submitter,
        log: ReplyLog,
        clients: Mapping[str, ShikiruClient],
        outbox: Outbox,
        clock: Clock,
        *,
        channel: str,
    ) -> None:
        self._submitter = submitter
        self._log = log
        self._clients = clients
        self._outbox = outbox
        self._clock = clock
        self._channel = channel

    async def handle(self, item: Inbound) -> None:
        destination = Destination(self._channel, str(item.sender))
        words = (item.text or "").split(maxsplit=_WORDS - 1)
        if len(words) < _WORDS or words[1] not in self._clients:
            names = ", ".join(sorted(self._clients))
            queue_command_reply(
                self._outbox,
                [destination],
                f"Usage: /goal <component> <request>. Components: {names}.",
                key=f"goal:usage:{item.update_id}",
                now=self._clock.now(),
            )
            return
        _, component, request = words
        payload = SubmitPayload(component=component, request=request, destination=destination)
        self._report(await self._submitter.submit(item.update_id, payload))

    async def tap(self, item: Inbound) -> None:
        """Retry the goal of a *Submit again* that was offered in the sender's chat."""
        destination = Destination(self._channel, str(item.sender))
        message = self._log.sent_message(destination, item.message_ref)
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
        op = await self._submitter.retry(callback[1], item.update_id)
        if op is not None:
            self._report(op)

    def _report(self, op: SubmitOp) -> None:
        destinations = [op.payload.destination]
        key = f"goal:{op.id}"
        now = self._clock.now()
        if op.status == SubmitStatus.CONFIRMED:
            text = f"Goal submitted as task {op.task_id}."
            queue_command_reply(self._outbox, destinations, text, key=key, now=now)
        elif op.status == SubmitStatus.FAILED:
            text = "Shikiru did not take this goal; it was not submitted."
            queue_command_reply(self._outbox, destinations, text, key=key, now=now)
        elif op.status == SubmitStatus.UNKNOWN:
            room = MESSAGE_TEXT_LIMIT - len(_ASK_HEAD) - len(_ASK_TAIL)
            text = f"{_ASK_HEAD}{fit(op.payload.request, room)}{_ASK_TAIL}"
            button = Button(_SUBMIT_AGAIN, submit_data(op.id))
            self._outbox.queue(
                None,
                Purpose.QUESTION,
                destinations,
                Message(text, (button,)),
                key=key,
                now=now,
            )
