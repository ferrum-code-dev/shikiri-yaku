from __future__ import annotations

from typing import TYPE_CHECKING, Final

from pydantic import PositiveInt, TypeAdapter, ValidationError

from shikiri_yaku.domain.callback import decode_callback
from shikiri_yaku.domain.inbound import InboundKind
from shikiri_yaku.domain.outbound import Purpose, refusal_notice
from shikiri_yaku.domain.secrets import secret_form
from shikiri_yaku.ports import (
    MESSAGE_TEXT_LIMIT,
    AgentOption,
    Button,
    EscalationStatus,
    Message,
    ResolutionKind,
    ResolvePayload,
    escalation_choices,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.ports import Clock, Outbox, ReplyLog, ReplyOrigin, ShikiruClient


_ANSWER: Final = "a"
_DETAILS: Final = "d"
_ACKNOWLEDGE: Final = "k"
_CONFIRM: Final = "confirm:"
_OP_ID = TypeAdapter[int](PositiveInt)


class Replies:
    """Apply stored inbound items through their originating messages and journaled resolve ops."""

    def __init__(
        self,
        log: ReplyLog,
        outbox: Outbox,
        clock: Clock,
        clients: Mapping[str, ShikiruClient],
        *,
        channel: str,
    ) -> None:
        self._log = log
        self._outbox = outbox
        self._clock = clock
        self._clients = clients
        self._channel = channel

    async def handle(self, item: Inbound) -> None:
        reference = item.reply_to_ref if item.kind == InboundKind.REPLY else item.message_ref
        origin = None if reference is None else self._log.origin(self._channel, reference)
        if origin is None or item.kind == InboundKind.TEXT:
            return
        if item.kind == InboundKind.REPLY:
            if origin.purpose == Purpose.QUESTION and item.text:
                await self._answer(item, origin, text=item.text)
            return
        await self._tap(item, origin)

    async def _tap(self, item: Inbound, origin: ReplyOrigin) -> None:
        if not item.data or item.data not in {button.data for button in origin.message.buttons}:
            return
        try:
            tap = decode_callback(item.data)
        except ValidationError:
            return
        verb, number, *options = tap
        if number != origin.number:
            return
        if verb == _ANSWER:
            if origin.purpose == Purpose.QUESTION:
                await self._answer(item, origin, option=options[0])
            elif origin.purpose == Purpose.APPROVAL:
                await self._confirm(origin)
        elif verb == _DETAILS and origin.purpose == Purpose.QUESTION:
            await self._details(item, origin)
        elif verb == _ACKNOWLEDGE and origin.purpose == Purpose.NOTICE:
            self._log.acknowledge_notice(origin.number, self._clock.now())

    async def _answer(
        self,
        item: Inbound,
        origin: ReplyOrigin,
        *,
        option: int | None = None,
        text: str | None = None,
    ) -> None:
        signal = origin.signal
        if signal.escalation_id is None:
            return
        view = await self._clients[signal.project].get_escalation(signal.escalation_id)
        if view is None or view.escalation.status != EscalationStatus.OPEN:
            return
        kind = ResolutionKind.ANSWER
        if option is not None:
            choice = next(
                (choice for choice in escalation_choices(view) if choice.number == option), None
            )
            if choice is None:
                return
            value = choice.value
            if isinstance(value, AgentOption):
                if value.needs_input:
                    self._input(item, origin)
                    return
                text = (
                    f"{value.label} ({value.consequences})" if value.consequences else value.label
                )
            else:
                kind = value.kind
                if kind == ResolutionKind.ANSWER:
                    self._input(item, origin)
                    return
        if kind not in {resolution.kind for resolution in view.resolutions}:
            return
        payload = ResolvePayload(
            number=origin.number,
            project=signal.project,
            task_id=signal.task_id,
            reason=signal.reason,
            escalation_id=signal.escalation_id,
            kind=kind,
            text=text,
        )
        await self._resolve(item, origin, payload)

    async def _resolve(self, item: Inbound, origin: ReplyOrigin, payload: ResolvePayload) -> None:
        op_id = self._log.intend_resolve(item.update_id, payload, self._clock.now())
        if op_id is None:
            self._queue(
                origin,
                Message(refusal_notice(origin.number)),
                Purpose.NOTICE,
                key=f"resolve:{item.update_id}:refused",
            )
            return
        if payload.kind in {ResolutionKind.ACCEPT, ResolutionKind.CANCEL}:
            self._queue(
                origin,
                Message(
                    f"S{origin.number} · Confirm {payload.kind}?",
                    (Button("Confirm", item.data or ""),),
                ),
                Purpose.APPROVAL,
                key=f"{_CONFIRM}{op_id}",
            )
        else:
            await self._issue(op_id)

    async def _issue(self, op_id: int) -> None:
        payload = self._log.claim_resolve(op_id, self._clock.now())
        if payload is None:
            return
        ack = await self._clients[payload.project].resolve_escalation(
            payload.escalation_id,
            payload.kind,
            payload.text,
        )
        self._log.acknowledge_resolve(op_id, ack, self._clock.now())

    def _input(self, item: Inbound, origin: ReplyOrigin) -> None:
        self._queue(
            origin,
            Message(f"S{origin.number} · Reply with your answer."),
            Purpose.QUESTION,
            key=f"input:{item.update_id}",
        )

    async def _confirm(self, origin: ReplyOrigin) -> None:
        if (
            origin.key is None
            or not origin.key.startswith(_CONFIRM)
            or origin.signal.escalation_id is None
        ):
            return
        view = await self._clients[origin.signal.project].get_escalation(
            origin.signal.escalation_id
        )
        if view is not None and view.escalation.status == EscalationStatus.OPEN:
            op_id = _OP_ID.validate_python(origin.key.removeprefix(_CONFIRM))
            if self._log.approve_resolve(op_id, origin.number, self._clock.now()):
                await self._issue(op_id)

    async def _details(self, item: Inbound, origin: ReplyOrigin) -> None:
        if origin.signal.escalation_id is None:
            return
        view = await self._clients[origin.signal.project].get_escalation(
            origin.signal.escalation_id
        )
        if view is None or view.packet is None:
            return
        if secret_form([view.packet]) is not None:
            self._queue(
                origin,
                Message(refusal_notice(origin.number)),
                Purpose.NOTICE,
                key=f"details:{item.update_id}:refused",
            )
            return
        for offset in range(0, len(view.packet), MESSAGE_TEXT_LIMIT):
            self._queue(
                origin,
                Message(view.packet[offset : offset + MESSAGE_TEXT_LIMIT]),
                Purpose.NOTICE,
                key=f"details:{item.update_id}:{offset}",
            )

    def _queue(self, origin: ReplyOrigin, message: Message, purpose: Purpose, *, key: str) -> None:
        self._outbox.queue(
            origin.number,
            purpose,
            [origin.destination],
            message,
            key=key,
            now=self._clock.now(),
        )
