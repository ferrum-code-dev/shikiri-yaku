"""Taps on records an upgraded journal still holds under the old command names act once.

Each test writes the records the version before the rename wrote, runs the repair startup runs
before any tap is looked up, and then taps. A repeated tap, or a repair run again, must not repeat
the effect.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.adapters.telegram import MESSAGE_TEXT_LIMIT, telegram_place
from shikiri_yaku.app.outbox_sender import OutboxSender
from shikiri_yaku.domain.callback import EXPERT_SELECTION_PREFIX, decline_data, install_data
from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.place import Place
from shikiri_yaku.journal import JournalFinalEdits, OpState, Repairs
from shikiri_yaku.journal.inbound import JournalInbound
from shikiri_yaku.ports import Button, Message
from tests.unit import test_ask, test_replies

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

    from shikiri_yaku.journal.replies import JournalReplies

NOW: Final = datetime(2026, 10, 28, tzinfo=UTC)
SENDER: Final = 5
CHAT: Final = -100123
OFFER_REF: Final = "-100123/1"


def _offer_before_rename(path: Path, *, admin: bool) -> tuple[test_ask.Harness, Inbound]:
    """Journal a request and the selection offer it was answered with, under the old key."""
    admins = frozenset({str(SENDER)}) if admin else frozenset[str]()
    h = test_ask.Harness(path, shared=True, admins=admins)
    command = "/admin" if admin else "/expert"
    request = Inbound(
        id=1,
        key=str(1),
        kind=InboundKind.TEXT,
        sender=str(SENDER),
        place=telegram_place(CHAT),
        message_ref="-100123/71",
        text=f"{command} question",
    )
    JournalInbound(h.database).record_batch("telegram", [request], "2", NOW)
    offer = Message(
        "Choose an Expert.",
        (Button(f"{command}@bot", f"{EXPERT_SELECTION_PREFIX}1"),),
        reply_to=request.message_ref,
    )
    h.outbox.queue_command_reply(
        [Place("telegram", str(CHAT))],
        [offer],
        key="ask:select:1",
        now=NOW,
    )
    channel = test_ask.Channel()
    asyncio.run(OutboxSender(h.outbox, channel, test_ask.Clock(), interval_s=1).tick())
    assert len(channel.sent) == 1
    return h, request


def _assert_selection_opens_once(path: Path, *, admin: bool) -> None:
    h, _ = _offer_before_rename(path, admin=admin)
    tap = Inbound(
        id=2,
        key=str(2),
        kind=InboundKind.TAP,
        sender=str(SENDER),
        place=telegram_place(CHAT),
        message_ref=OFFER_REF,
        data=f"{EXPERT_SELECTION_PREFIX}1",
    )

    assert Repairs(h.database).rename_keys() > 0
    assert asyncio.run(h.commands.tap(tap))
    assert Repairs(h.database).rename_keys() == 0
    assert asyncio.run(h.commands.tap(tap))

    assert len(h.topics.created) == len(h.expert.requests) == 1
    [asked] = h.expert.requests
    assert asked.admin is admin
    final = JournalFinalEdits(h.database, cast("JournalReplies", None), max_text=MESSAGE_TEXT_LIMIT)
    edits = {edit.message_ref: edit.message for edit in final.to_edit()}
    assert edits[OFFER_REF].buttons[0].url == "https://t.me/c/123/22"


def test_a_selection_of_an_expert_offer_from_before_the_rename_opens_one_conversation(
    tmp_path: Path,
) -> None:
    _assert_selection_opens_once(tmp_path, admin=False)


def test_a_selection_of_an_admin_offer_from_before_the_rename_opens_one_conversation(
    tmp_path: Path,
) -> None:
    _assert_selection_opens_once(tmp_path, admin=True)


@pytest.fixture
def replies() -> Generator[test_replies.Harness]:
    harness = test_replies.Harness()
    yield harness
    harness.database.close()


_DECLINED: Final = "declined the install approval"


@pytest.mark.parametrize(
    ("answer", "state", "forwards"),
    [(install_data, OpState.APPROVED, 0), (decline_data, OpState.DECLINED, 1)],
    ids=["approve", "decline"],
)
def test_a_re_shown_install_approval_from_before_the_rename_applies_once(
    replies: test_replies.Harness,
    answer: Callable[[int, int], str],
    state: OpState,
    forwards: int,
) -> None:
    handler, op_id = replies.install_replies()
    replies.origin(
        "1001/50",
        Purpose.APPROVAL,
        (install_data(replies.number, op_id), decline_data(replies.number, op_id)),
        key=f"install:{op_id}:open:700",
        at=NOW,
    )
    data = answer(replies.number, op_id)

    replies.handle(update=700, ref="1001/50", data=data, install_replies=handler)
    assert replies.runner.started == []

    assert Repairs(replies.database).rename_keys() > 0
    replies.handle(update=701, ref="1001/50", data=data, install_replies=handler)
    replies.handle(update=701, ref="1001/50", data=data, install_replies=handler)
    assert Repairs(replies.database).rename_keys() == 0
    replies.handle(update=702, ref="1001/50", data=data, install_replies=handler)

    operation = replies.ops.install_op(op_id)
    assert operation is not None
    assert operation.state == state
    assert replies.runner.started == ([op_id] if state is OpState.APPROVED else [])
    declined = [
        message
        for message in replies.messages.for_signal(replies.number)
        if message.body is not None and _DECLINED in message.body.text
    ]
    assert len(declined) == forwards
