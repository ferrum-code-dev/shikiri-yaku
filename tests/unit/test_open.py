from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest

from shikiri_yaku.app.replies import Replies
from shikiri_yaku.bootstrap import goal_command
from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.journal import JournalFinalEdits, JournalReplacements
from shikiri_yaku.journal.signals import NewSignal, SignalState
from shikiri_yaku.ports import EscalationStatus, ShikiruClient, ShikiruLinkDownError
from tests.support.destinations import FixedDestinations
from tests.unit.test_replies import NOW, Client, Clock, Harness

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.ports import EscalationView


class _Silent(Client):
    async def list_escalations(self) -> list[EscalationView]:
        message = "link is down"
        raise ShikiruLinkDownError(message)


class _Raced:
    def __init__(self, view: EscalationView, detail: EscalationView | None) -> None:
        self._view = view
        self._detail = detail

    async def list_escalations(self) -> list[EscalationView]:
        return [self._view]

    async def get_escalation(self, escalation_id: str) -> EscalationView | None:
        assert escalation_id == "E-1"
        return self._detail


def _resolved(view: EscalationView) -> EscalationView:
    record = view.escalation.model_copy(update={"status": "resolved"})
    return view.model_copy(update={"escalation": record})


@pytest.fixture
def h() -> Generator[Harness]:
    harness = Harness()
    harness.signals.transition(harness.number, SignalState.NEW, SignalState.FORWARDED, NOW)
    yield harness
    harness.database.close()


def _open(h: Harness, update: int = 700) -> None:
    replies = Replies(
        h.log(),
        h.outbox,
        Clock(NOW),
        h.clients,
        channel="telegram",
        commands=h.commands(NOW),
        goal=goal_command(h.database, h.clients, h.outbox, Clock(NOW), FixedDestinations()),
    )
    asyncio.run(
        replies.handle(Inbound(update, InboundKind.TEXT, 1001, 1001, "1001/90", text="/open"))
    )


def _deliver(h: Harness) -> list[str]:
    refs: list[str] = []
    for pending in h.outbox.pending():
        ref = f"1001/{pending.message_id}"
        h.outbox.confirm(pending.message_id, ref)
        refs.append(ref)
    return refs


def _stored(h: Harness) -> list[tuple[int | None, Purpose | None]]:
    return [(stored.signal_number, stored.purpose) for stored in h.messages.unconfirmed()]


def _reopen_in_shikiru(h: Harness) -> None:
    record = h.client.view.escalation.model_copy(update={"status": EscalationStatus.OPEN})
    h.client.view = h.client.view.model_copy(update={"escalation": record})


def test_replaying_the_same_open_update_re_shows_nothing_new(h: Harness) -> None:
    h.handle(data="a:1:4")
    _deliver(h)
    _open(h, update=700)
    _open(h, update=700)
    assert len(h.outbox.pending()) == 2


def test_a_replayed_open_update_answers_nothing_more_after_its_question_is_resolved(
    h: Harness,
) -> None:
    _open(h, update=700)
    h.resolve_in_shikiru()
    _open(h, update=700)
    assert _stored(h) == [(h.number, Purpose.QUESTION)]


def test_a_replayed_open_update_does_not_add_a_question_opened_after_its_nothing_answer(
    h: Harness,
) -> None:
    h.resolve_in_shikiru()
    _open(h, update=700)
    _reopen_in_shikiru(h)
    _open(h, update=700)
    assert _stored(h) == [(None, Purpose.NOTICE)]


def test_an_open_interrupted_before_it_is_answered_is_finished_by_its_replay(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = h.outbox.queue

    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(h.outbox, "queue", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _open(h, update=700)
    monkeypatch.setattr(h.outbox, "queue", queue)
    _open(h, update=700)
    assert _stored(h) == [(h.number, Purpose.QUESTION)]


def test_a_later_open_shows_the_open_items_again(h: Harness) -> None:
    _open(h, update=700)
    _deliver(h)
    _open(h, update=701)
    assert [stored.purpose for stored in h.messages.unconfirmed()] == [Purpose.QUESTION]


def test_a_question_shown_again_by_open_replaces_the_copy_it_repeats(h: Harness) -> None:
    _open(h, update=700)
    [first] = _deliver(h)
    _open(h, update=701)
    _deliver(h)
    assert JournalReplacements(h.database, h.messages).superseded() == [first]


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
    assert _stored(h) == [(None, Purpose.NOTICE)]


def test_an_escalation_resolved_elsewhere_is_not_shown_open(h: Harness) -> None:
    h.handle(data="a:1:4")
    _deliver(h)
    h.resolve_in_shikiru()
    _open(h)
    assert _stored(h) == [(None, Purpose.NOTICE)]


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


@pytest.mark.parametrize("copy_first", [False, True])
def test_an_approval_left_approved_by_a_crash_is_shown_again_and_issues_once(
    h: Harness, *, copy_first: bool
) -> None:
    h.handle(data="a:1:4")
    [original] = _deliver(h)
    (op,) = h.ops.non_final()
    # keep: a crash after the approval and before the issue leaves the op APPROVED
    approved = h.log().approve_resolve(op.id, h.number, NOW)
    assert approved
    _open(h)
    assert [stored.purpose for stored in h.messages.unconfirmed()] == [
        Purpose.QUESTION,
        Purpose.APPROVAL,
    ]
    assert h.client.calls == []
    _, copy = _deliver(h)
    first, second = (copy, original) if copy_first else (original, copy)
    h.handle(update=701, ref=first, data="a:1:4")
    h.handle(update=702, ref=second, data="a:1:4")
    assert h.client.calls == [("E-1", "cancel", None)]
    assert [stored.id for stored in h.ops.non_final()] == [op.id]


def test_a_question_listed_open_but_resolved_on_its_detail_is_not_shown(h: Harness) -> None:
    h.clients["api"] = cast("ShikiruClient", _Raced(h.client.view, _resolved(h.client.view)))
    _open(h)
    assert _stored(h) == [(None, Purpose.NOTICE)]


def test_a_question_missing_on_its_detail_fetch_is_not_shown_and_only_the_nothing_answer_is_sent(
    h: Harness,
) -> None:
    h.clients["api"] = cast("ShikiruClient", _Raced(h.client.view, None))
    _open(h)
    assert _stored(h) == [(None, Purpose.NOTICE)]
