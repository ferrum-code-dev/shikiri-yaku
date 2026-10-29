"""Answers `/goal <component> <request>`: submits the goal and reports the outcome (§7, §10.1)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from shikiri_yaku.app.command_reply import item_destination, queue_command_reply
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
    from shikiri_yaku.ports import Clock, Outbox, ReplyLog, ShikiruClient, SubmitOp, TopicLog

_WORDS: Final = 3
_TOPIC_WORDS: Final = 2
_ASK_HEAD: Final = "I may have submitted this already: "
_ASK_TAIL: Final = ". Check /status, or tap Submit again."
_SUBMIT_AGAIN: Final = "Submit again"


class GoalCommand:
    """Submits a goal and reports its outcome in the chat and topic it was sent from.

    The destination is stored with the goal, so the outcome and any question go where the goal was
    sent. A goal sent in a component's topic implies that component. An unknown outcome is asked
    about with *Submit again*; only that tap retries the goal.
    """

    def __init__(
        self,
        submitter: Submitter,
        log: ReplyLog,
        clients: Mapping[str, ShikiruClient],
        outbox: Outbox,
        clock: Clock,
        *,
        topics: TopicLog,
        channel: str,
    ) -> None:
        self._submitter = submitter
        self._log = log
        self._clients = clients
        self._outbox = outbox
        self._clock = clock
        self._topics = topics
        self._channel = channel

    async def handle(self, item: Inbound) -> None:
        destination = item_destination(self._channel, item)
        parsed = self._parse(item)
        if parsed is None or parsed[0] not in self._clients:
            names = ", ".join(sorted(self._clients))
            queue_command_reply(
                self._outbox,
                [destination],
                f"Usage: /goal <component> <request>. Components: {names}.",
                key=f"goal:usage:{item.update_id}",
                now=self._clock.now(),
            )
            return
        component, request = parsed
        payload = SubmitPayload(component=component, request=request, destination=destination)
        self._report(await self._submitter.submit(item.update_id, payload))

    async def tap(self, item: Inbound) -> None:
        """Retry the goal of a *Submit again* that was offered in the chat of the tap."""
        destination = Destination(self._channel, str(item.chat))
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

    def recover(self) -> None:
        """Ask about each goal an earlier process left unknown; it runs before any inbound item."""
        for op in self._submitter.recover():
            self._report(op)

    def _parse(self, item: Inbound) -> tuple[str, str] | None:
        """Return the component and request of a goal, or None when it has no request.

        In a component's topic the whole text after `/goal` is the request; elsewhere the text is
        `/goal <component> <request>`.
        """
        implied = None if item.thread is None else self._topics.component_of(item.thread)
        if implied is not None:
            words = (item.text or "").split(maxsplit=1)
            return None if len(words) < _TOPIC_WORDS else (implied, words[1])
        words = (item.text or "").split(maxsplit=_WORDS - 1)
        if len(words) < _WORDS:
            return None
        _, component, request = words
        return component, request

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
