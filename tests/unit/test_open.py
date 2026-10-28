from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest

from shikiri_yaku.app.replies import Replies
from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.journal import JournalFinalEdits
from shikiri_yaku.journal.signals import NewSignal, SignalState
from shikiri_yaku.ports import ShikiruClient, ShikiruLinkDownError
from tests.unit.test_replies import NOW, Client, Clock, Harness

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.ports import EscalationView


class _Silent(Client):
    async def list_escalations(self) -> list[EscalationView]:
        message = "link is down"
        raise ShikiruLinkDownError(message)


@pytest.fixture
def h() -> Generator[Harness]:
    harness = Harness()
    harness.signals.transition(harness.number, SignalState.NEW, SignalState.FORWARDED, NOW)
    yield harness
    harness.database.close()


def _open(h: Harness, update: int = 700) -> None:
    replies = Replies(
        h.log(), h.outbox, Clock(NOW), h.clients, channel="telegram", commands=h.commands(NOW)
    )
    asyncio.run(replies.handle(Inbound(update, InboundKind.TEXT, 1001, "1001/90", text="/open")))


def _deliver(h: Harness) -> list[str]:
    refs: list[str] = []
    for pending in h.outbox.pending():
        ref = f"1001/{pending.message_id}"
        h.outbox.confirm(pending.message_id, ref)
        refs.append(ref)
    return refs


def test_replaying_the_same_open_update_re_shows_nothing_new(h: Harness) -> None:
    h.handle(data="a:1:4")
    _deliver(h)
    _open(h, update=700)
    _open(h, update=700)
    assert len(h.outbox.pending()) == 2


def test_a_later_open_shows_the_open_items_again(h: Harness) -> None:
    _open(h, update=700)
    _deliver(h)
    _open(h, update=701)
    assert [stored.purpose for stored in h.messages.unconfirmed()] == [Purpose.QUESTION]


def test_two_confirm_copies_of_one_approval_issue_one_resolve(h: Harness) -> None:
    h.handle(data="a:1:4")
    [original] = _deliver(h)
    _open(h)
    *_, copy = _deliver(h)
    h.handle(update=701, ref=original, data="a:1:4")
    h.handle(update=702, ref=copy, data="a:1:4")
    assert h.client.calls == [("E-1", "cancel", None)]


def test_an_approval_already_issued_is_not_shown_again(h: Harness) -> None:
    h.handle(data="a:1:4")
    [original] = _deliver(h)
    h.handle(update=701, ref=original, data="a:1:4")
    _open(h)
    assert h.messages.unconfirmed() == []


def test_an_escalation_resolved_elsewhere_is_not_shown_open(h: Harness) -> None:
    h.handle(data="a:1:4")
    _deliver(h)
    h.resolve_in_shikiru()
    _open(h)
    assert h.outbox.pending() == []


def test_a_tap_on_a_copy_of_an_escalation_resolved_elsewhere_does_not_resolve(h: Harness) -> None:
    h.handle(data="a:1:4")
    _deliver(h)
    _open(h)
    *_, copy = _deliver(h)
    h.resolve_in_shikiru()
    h.handle(update=701, ref=copy, data="a:1:4")
    assert h.client.calls == []


def test_a_project_that_does_not_answer_is_skipped_and_the_sender_is_told(h: Harness) -> None:
    web = h.signals.insert_if_absent(
        NewSignal(
            "web:escalation:E-2", "web", "escalation", "decision_required", "question", "T-2", "E-2"
        ),
        NOW,
    ).row_id
    h.signals.transition(web, SignalState.NEW, SignalState.FORWARDED, NOW)
    h.clients["web"] = cast("ShikiruClient", _Silent())
    _open(h)
    assert [(stored.signal_number, stored.purpose) for stored in h.messages.unconfirmed()] == [
        (h.number, Purpose.QUESTION),
        (None, Purpose.NOTICE),
    ]


def test_a_re_shown_copy_takes_the_final_edit_like_the_original(h: Harness) -> None:
    _open(h)
    [copy] = _deliver(h)
    edits = JournalFinalEdits(h.database, h.log())
    edits.settle(h.number, "Resolved in Shikiri: Keep existing behavior")
    assert copy in {edit.message_ref for edit in edits.to_edit()}
