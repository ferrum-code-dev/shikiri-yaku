from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from shikiri_yaku.app.replies import Replies
from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.journal import (
    Database,
    JournalOutbox,
    MessageStore,
    OpState,
    OpStore,
    SignalStore,
)
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.journal.signals import NewSignal
from shikiri_yaku.ports import (
    Ack,
    Button,
    EscalationView,
    Message,
    ResolvePayload,
    ShikiruClient,
    ShikiruLinkDownError,
    ShikiruRejectedError,
)

if TYPE_CHECKING:
    from collections.abc import Generator

NOW = datetime(2026, 10, 28, tzinfo=UTC)


class Client:
    def __init__(self) -> None:
        fixture = Path(__file__).parents[1] / "fixtures/shikiru/get_escalation.json"
        self.view = EscalationView.model_validate(json.loads(fixture.read_text())["result"])
        self.calls: list[tuple[str, str, str | None]] = []
        self.fail = False
        self.reject = False

    async def get_escalation(self, escalation_id: str) -> EscalationView:
        assert escalation_id == "E-1"
        return self.view

    async def resolve_escalation(self, escalation_id: str, kind: str, text: str | None) -> Ack:
        self.calls.append((escalation_id, kind, text))
        if self.fail:
            message = "link lost before acknowledgement"
            raise ShikiruLinkDownError(message)
        if self.reject:
            message = "escalation already resolved"
            raise ShikiruRejectedError(message)
        return Ack(id=12, status="queued", result=None, message=None)


class Clock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class Reporter:
    def report(self, line: str) -> None:
        pytest.fail(line)


class Harness:
    def __init__(self) -> None:
        self.database = Database.open(":memory:")
        self.signals = SignalStore(self.database)
        self.number = self.signals.insert_if_absent(
            NewSignal(
                "api:escalation:E-1",
                "api",
                "escalation",
                "decision_required",
                "question",
                "T-1",
                "E-1",
            ),
            NOW,
        ).row_id
        self.messages = MessageStore(self.database, Reporter())
        self.outbox = JournalOutbox(self.database, self.messages)
        self.ops = OpStore(self.database)
        self.client = Client()
        self.clients = {"api": cast("ShikiruClient", self.client)}
        self.origin("1001/42", Purpose.QUESTION)

    def origin(
        self,
        ref: str,
        purpose: Purpose,
        data: tuple[str, ...] = ("a:1:1", "a:1:2", "a:1:3", "a:1:4"),
    ) -> None:
        row = self.messages.record_outbound(
            signal_number=self.number,
            channel="telegram",
            chat="1001",
            purpose=purpose,
            body=Message("question", tuple(Button("Choice", value) for value in data)),
            notice_key=None,
            now=NOW,
        )
        self.messages.confirm_delivery(row.row_id, ref)

    def log(self) -> JournalReplies:
        return JournalReplies(self.database, self.signals, OpStore(self.database))

    def handle(
        self,
        update: int = 700,
        ref: str = "1001/42",
        data: str | None = None,
        text: str | None = None,
    ) -> None:
        replies = Replies(self.log(), self.outbox, Clock(), self.clients, channel="telegram")
        item = Inbound(
            update,
            InboundKind.TAP if text is None else InboundKind.REPLY,
            1001,
            ref if text is None else "1001/90",
            reply_to_ref=ref if text else None,
            text=text,
            data=data,
        )
        asyncio.run(replies.handle(item))


@pytest.fixture
def h() -> Generator[Harness]:
    harness = Harness()
    yield harness
    harness.database.close()


@pytest.mark.parametrize("text", [None, "Use the other approach"])
def test_answer_duplicate_after_reconstruction_issues_once(h: Harness, text: str | None) -> None:
    h.handle(data="a:1:1", text=text)
    h.handle(data="a:1:1", text=text)
    assert h.client.calls == [
        ("E-1", "answer", text or "Keep existing behavior (Preserve the current call pattern.)")
    ]
    (op,) = h.ops.non_final()
    assert op.state == OpState.ISSUED
    assert op.command_id == "12"
    assert op.acknowledgement is not None
    assert op.acknowledgement.status == "queued"


def test_retry_uses_fresh_view_and_shared_numbering_gaps(h: Harness) -> None:
    resolution = h.client.view.resolutions[0]
    h.client.view = h.client.view.model_copy(
        update={
            "agent_options": [],
            "resolutions": [
                resolution.model_copy(update={"kind": "future"}),
                resolution.model_copy(update={"kind": "retry"}),
            ],
        }
    )
    h.handle(data="a:1:2")
    assert h.client.calls == [("E-1", "retry", None)]


@pytest.mark.parametrize(
    ("purpose", "text", "data"),
    [
        (Purpose.APPROVAL, "yes", None),
        (Purpose.APPROVAL, None, "a:1:2"),
        (Purpose.NOTICE, "answer", None),
        (Purpose.QUESTION, None, "a:1:2"),
    ],
)
def test_purpose_and_stored_buttons_bind_answers(
    h: Harness, purpose: Purpose, text: str | None, data: str | None
) -> None:
    h.origin("1001/43", purpose, ("a:1:1",))
    h.handle(ref="1001/43", text=text, data=data)
    assert h.client.calls == []


@pytest.mark.parametrize(
    ("ref", "data"),
    [("1002/42", "a:1:1"), ("1001/44", "a:2:1"), ("1001/44", "a:1"), ("1001/42", "a:1:99")],
)
def test_unknown_origin_wrong_signal_and_invalid_buttons_do_not_resolve(
    h: Harness, ref: str, data: str
) -> None:
    h.origin("1001/44", Purpose.QUESTION, ("a:2:1", "a:1"))
    h.handle(ref=ref, data=data)
    assert h.client.calls == []


@pytest.mark.parametrize("data", ["a:1:1", "a:1:3", "a:1:4"])
def test_input_and_confirmation_taps_do_not_resolve_immediately(h: Harness, data: str) -> None:
    option = h.client.view.agent_options[0].model_copy(update={"needs_input": True})
    h.client.view = h.client.view.model_copy(
        update={"agent_options": [option, h.client.view.agent_options[1]]}
    )
    h.handle(data=data)
    assert all(op.state == OpState.INTENDED for op in h.ops.non_final())
    assert h.client.calls == []


def test_claimed_failure_is_left_for_reconciliation(h: Harness) -> None:
    h.client.fail = True
    with pytest.raises(ShikiruLinkDownError):
        h.handle(data="a:1:1")
    h.handle(data="a:1:1")
    assert len(h.client.calls) == 1
    (op,) = h.ops.non_final()
    assert op.state == OpState.ISSUED
    assert op.acknowledgement is None


def test_rejection_fails_the_op_and_shows_shikirus_message(h: Harness) -> None:
    h.client.reject = True
    h.handle(data="a:1:1")
    assert h.ops.non_final() == []
    (pending,) = h.outbox.pending()
    assert "escalation already resolved" in pending.message.text
    h.handle(data="a:1:1")
    assert len(h.client.calls) == 1


def test_queued_acknowledgement_keeps_the_op_issued_with_its_command_id(h: Harness) -> None:
    h.handle(data="a:1:1")
    (op,) = h.ops.non_final()
    assert (op.state, op.command_id) == (OpState.ISSUED, "12")


def test_issuance_uses_all_stored_fields_including_project(h: Harness) -> None:
    other = Client()
    h.clients["web"] = cast("ShikiruClient", other)
    payload = ResolvePayload(
        number=1,
        project="web",
        task_id="T-2",
        reason="agent_error",
        escalation_id="E-2",
        kind="retry",
        text=None,
    )
    h.log().intend_resolve(700, payload, NOW)
    h.handle(data="a:1:1")
    assert h.client.calls == []
    assert other.calls == [("E-2", "retry", None)]
