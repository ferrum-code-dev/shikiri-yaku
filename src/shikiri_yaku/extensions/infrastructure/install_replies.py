"""Replies to an Install approval, with its original op as the authority."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Final, Literal

from shikiri_yaku.domain.callback import decline_data, install_data
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.ports import (
    INSTALL_DECLINED,
    INSTALL_EXPIRED,
    INSTALL_INTENDED,
    INSTALL_VOID,
    EscalationStatus,
    Message,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from shikiri_yaku.domain.inbound import Inbound
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import (
        Clock,
        Forwarder,
        InstallLog,
        InstallOp,
        InstallRunner,
        Outbox,
        ReplyOrigin,
        ShikiruClient,
    )

DECLINED_NOTE: Final = "The Product Owner declined the install approval; no host command was run."
EXPIRED_NOTE: Final = "This install approval expired; no host command was run."
VOID_NOTE: Final = "This escalation is no longer open; the install approval is void."
_INSTALL: Final = "i"
_DECLINE: Final = "x"


class InstallApprovalReplies:
    """Apply a bound button once, expire it from its op creation, and safely replay declines."""

    def __init__(
        self,
        installs: InstallLog,
        clients: Mapping[str, ShikiruClient],
        forwarder: Forwarder,
        outbox: Outbox,
        clock: Clock,
        *,
        approval_expiry_h: int,
        runner: InstallRunner,
    ) -> None:
        self._installs = installs
        self._clients = clients
        self._forwarder = forwarder
        self._outbox = outbox
        self._clock = clock
        self._expiry_h = approval_expiry_h
        self._runner = runner

    async def handle(
        self,
        item: Inbound,
        origin: ReplyOrigin,
        *,
        verb: Literal["i", "x"],
        op_id: int,
    ) -> None:
        operation = self._installs.install_op(op_id)
        if operation is None or not _bound(operation, item, origin, verb, op_id):
            return
        if operation.state != INSTALL_INTENDED:
            await self._replay_effect(operation.state, origin, verb)
            return
        await self._decide(operation, origin, verb)

    async def _replay_effect(
        self, state: str, origin: ReplyOrigin, verb: Literal["i", "x"]
    ) -> None:
        if state == INSTALL_DECLINED and verb == _DECLINE:
            await self._forward(origin)
        elif state == INSTALL_EXPIRED:
            self._notice(origin, origin.key, INSTALL_EXPIRED, EXPIRED_NOTE)
        elif state == INSTALL_VOID:
            self._notice(origin, origin.key, INSTALL_VOID, VOID_NOTE)

    async def _decide(
        self, operation: InstallOp, origin: ReplyOrigin, verb: Literal["i", "x"]
    ) -> None:
        now = self._clock.now()
        if now - operation.created_at >= timedelta(hours=self._expiry_h):
            self._expire(operation, origin, now)
            return
        if not await self._is_open(origin.signal):
            self._void(operation, origin, now)
            return

        if verb == _INSTALL:
            if self._installs.approve_install(operation.id, origin.number, now):
                self._runner.start(operation.id)
        elif self._installs.decline_install(operation.id, origin.number, now):
            await self._forward(origin)

    def _expire(self, operation: InstallOp, origin: ReplyOrigin, now: datetime) -> None:
        if self._installs.expire_install(operation.id, origin.number, now):
            self._notice(origin, origin.key, INSTALL_EXPIRED, EXPIRED_NOTE)

    def _void(self, operation: InstallOp, origin: ReplyOrigin, now: datetime) -> None:
        if self._installs.void_install(operation.id, origin.number, now):
            self._notice(origin, origin.key, INSTALL_VOID, VOID_NOTE)

    async def _is_open(self, signal: Signal) -> bool:
        if signal.escalation_id is None:
            return False
        view = await self._clients[signal.project].get_escalation(signal.escalation_id)
        return view is not None and view.escalation.status == EscalationStatus.OPEN

    async def _forward(self, origin: ReplyOrigin) -> None:
        await self._forwarder.forward(
            origin.number, origin.signal, DECLINED_NOTE, origin.suggestion
        )

    def _notice(self, origin: ReplyOrigin, approval_key: str | None, state: str, note: str) -> None:
        key = f"{approval_key}:{state}"
        self._outbox.queue(
            origin.number,
            Purpose.NOTICE,
            [origin.destination],
            Message(f"S{origin.number} · {note}"),
            key=key,
            now=self._clock.now(),
        )


def _bound(operation: InstallOp, item: Inbound, origin: ReplyOrigin, verb: str, op_id: int) -> bool:
    expected = (
        install_data(origin.number, op_id)
        if verb == _INSTALL
        else decline_data(origin.number, op_id)
    )
    return (
        origin.purpose == Purpose.APPROVAL
        and _install_key(origin.key, op_id)
        and item.data == expected
        and expected in {button.data for button in origin.message.buttons}
        and operation.identity == f"install:{origin.signal.id}"
        and operation.project == origin.signal.project
        and operation.task_id == origin.signal.task_id
        and operation.reason == origin.signal.reason
        and operation.id == op_id
        and operation.payload.number == origin.number
        and origin.signal.escalation_id == operation.payload.escalation_id
    )


def _install_key(key: str | None, op_id: int) -> bool:
    original = f"install:{op_id}"
    if key == original:
        return True
    prefix = f"{original}:open:"
    update_id = "" if key is None else key.removeprefix(prefix)
    return (
        key is not None and key.startswith(prefix) and update_id.isascii() and update_id.isdecimal()
    )
