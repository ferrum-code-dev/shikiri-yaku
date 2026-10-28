from __future__ import annotations

from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from shikiri_yaku.domain.callback import decode_callback
from shikiri_yaku.domain.inbound import InboundKind
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.ports import (
    AgentOption,
    EscalationStatus,
    ResolutionKind,
    ResolvePayload,
    escalation_choices,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.ports import Clock, ReplyLog, ReplyOrigin, ShikiruClient


_ANSWER: Final = "a"


class Replies:
    """Apply stored inbound items through their originating messages and journaled resolve ops."""

    def __init__(
        self,
        log: ReplyLog,
        clock: Clock,
        clients: Mapping[str, ShikiruClient],
        *,
        channel: str,
    ) -> None:
        self._log = log
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
        if not item.data or item.data not in {button.data for button in origin.message.buttons}:
            return
        try:
            tap = decode_callback(item.data)
        except ValidationError:
            return
        verb, number, *options = tap
        if number == origin.number and verb == _ANSWER and origin.purpose == Purpose.QUESTION:
            await self._answer(item, origin, option=options[0])

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
                    return
                text = (
                    f"{value.label} ({value.consequences})" if value.consequences else value.label
                )
            else:
                kind = value.kind
                if kind != ResolutionKind.RETRY:
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
        op_id = self._log.intend_resolve(item.update_id, payload, self._clock.now())
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
