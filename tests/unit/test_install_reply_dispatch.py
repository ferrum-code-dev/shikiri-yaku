from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal, cast

from shikiri_yaku.app.replies import Replies
from shikiri_yaku.domain.callback import decline_data, install_data
from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.ports import Button, Message, ReplyOrigin

if TYPE_CHECKING:
    from shikiri_yaku.app.commands import Commands
    from shikiri_yaku.app.goal import GoalCommand
    from shikiri_yaku.ports import (
        Clock,
        InstallReplyHandler,
        Outbox,
        ReplyLog,
    )


class Origins:
    def __init__(self, origins: dict[str, ReplyOrigin]) -> None:
        self._origins = origins

    def origin(self, channel: str, message_ref: str) -> ReplyOrigin | None:
        assert channel == "telegram"
        return self._origins.get(message_ref)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def handle(
        self,
        item: Inbound,
        origin: ReplyOrigin,
        *,
        verb: Literal["i", "x"],
        op_id: int,
    ) -> None:
        del item, origin
        self.calls.append((verb, op_id))


def test_only_install_buttons_from_approval_origins_are_dispatched() -> None:
    signal = Signal("api", Kind.ESCALATION, "E-1", "decision_required", "question")
    buttons = (Button("Install", install_data(1, 8)), Button("Decline", decline_data(1, 8)))
    approval = ReplyOrigin(
        1,
        signal,
        Purpose.APPROVAL,
        Message("approval", buttons),
        "install:8",
        Destination("telegram", "1001"),
    )
    notice = ReplyOrigin(
        1,
        signal,
        Purpose.NOTICE,
        Message("notice", buttons),
        "notice",
        Destination("telegram", "1001"),
    )
    handler = Recorder()
    replies = Replies(
        cast("ReplyLog", Origins({"approval": approval, "notice": notice})),
        cast("Outbox", object()),
        cast("Clock", object()),
        {},
        channel="telegram",
        commands=cast("Commands", object()),
        goal=cast("GoalCommand", object()),
        install_replies=cast("InstallReplyHandler", handler),
    )

    async def handle_items() -> None:
        await replies.handle(
            Inbound(1, InboundKind.TAP, 1, 1001, "approval", data=install_data(1, 8))
        )
        await replies.handle(
            Inbound(2, InboundKind.TAP, 1, 1001, "approval", data=decline_data(1, 8))
        )
        await replies.handle(Inbound(3, InboundKind.TAP, 1, 1001, "approval", data="a:1:1"))
        await replies.handle(
            Inbound(4, InboundKind.TAP, 1, 1001, "notice", data=install_data(1, 8))
        )
        await replies.handle(
            Inbound(
                5,
                InboundKind.REPLY,
                1,
                1001,
                "reply",
                reply_to_ref="approval",
                text="please install it",
            )
        )

    asyncio.run(handle_items())

    assert handler.calls == [("i", 8), ("x", 8)]
