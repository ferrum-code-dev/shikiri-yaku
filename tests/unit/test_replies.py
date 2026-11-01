from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from shikiri_yaku.app.commands import Commands
from shikiri_yaku.app.copy_editor import CopyEditor
from shikiri_yaku.app.open_command import OpenCommand
from shikiri_yaku.app.replies import Replies
from shikiri_yaku.app.status import Status
from shikiri_yaku.bootstrap import SILENCING_PREFIXES, goal_command
from shikiri_yaku.domain.callback import decline_data, install_data
from shikiri_yaku.domain.classification import InstallProposal, Verdict
from shikiri_yaku.domain.inbound import Inbound, InboundKind
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.recipes import InstallOpPayload
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.extensions.infrastructure.install import InstallApprovalPresenter
from shikiri_yaku.extensions.infrastructure.install_replies import InstallApprovalReplies
from shikiri_yaku.extensions.infrastructure.rules import INFRASTRUCTURE
from shikiri_yaku.extensions.product.forwarder import ProductForwarder
from shikiri_yaku.journal import (
    CursorStore,
    Database,
    JournalFinalEdits,
    JournalOutbox,
    JournalSources,
    MessageStore,
    OpState,
    OpStore,
    SignalState,
    SignalStore,
)
from shikiri_yaku.journal.open import JournalOpen
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.journal.signals import NewSignal
from shikiri_yaku.ports import (
    MESSAGE_TEXT_LIMIT,
    Ack,
    Button,
    EscalationView,
    Message,
    ResolvePayload,
    ShikiruClient,
    ShikiruLinkDownError,
    ShikiruRejectedError,
    TaskDetail,
)
from shikiri_yaku.settings import (
    BUTTON_LABEL_MAX_CHARS,
    COMMAND_NAME_MAX_CHARS,
    MESSAGE_TITLE_MAX_CHARS,
    InfrastructureSettings,
)
from tests.support.destinations import FixedDestinations

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.ports import InstallReplyHandler

NOW = datetime(2026, 10, 28, tzinfo=UTC)
ANSWERED_AT = datetime(2026, 10, 28, 12, 3, tzinfo=UTC)
LATER_AT = datetime(2026, 10, 28, 13, 30, tzinfo=UTC)
OWNER = Destination("telegram", "1001")


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

    async def get_task(self, task_id: str) -> TaskDetail | None:
        del task_id
        return None

    async def list_escalations(self) -> list[EscalationView]:
        return [self.view]

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
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class Reporter:
    def report(self, line: str) -> None:
        pytest.fail(line)


class Runner:
    def __init__(self) -> None:
        self.started: list[int] = []

    def start(self, op_id: int) -> None:
        self.started.append(op_id)


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
        self.runner = Runner()
        self.origin("1001/42", Purpose.QUESTION)

    def origin(
        self,
        ref: str,
        purpose: Purpose,
        data: tuple[str, ...] = ("a:1:1", "a:1:2", "a:1:3", "a:1:4"),
        *,
        key: str | None = None,
        at: datetime = NOW,
    ) -> None:
        row = self.messages.record_outbound(
            signal_number=self.number,
            channel="telegram",
            chat="1001",
            purpose=purpose,
            body=Message("question", tuple(Button("Choice", value) for value in data)),
            notice_key=key,
            now=at,
        )
        self.messages.confirm_delivery(row.row_id, ref)

    def log(self) -> JournalReplies:
        return JournalReplies(
            self.database,
            self.signals,
            OpStore(self.database),
            silencing_prefixes=SILENCING_PREFIXES,
        )

    def commands(self, at: datetime) -> Commands:
        sources = JournalSources(CursorStore(self.database, self.signals), self.signals)
        status = Status(self.clients, sources)
        reshower = ProductForwarder(
            MESSAGE_TITLE_MAX_CHARS,
            self.clients,
            self.outbox,
            Clock(at),
            FixedDestinations([OWNER]),
            button_label_max_chars=BUTTON_LABEL_MAX_CHARS,
        )
        opened = OpenCommand(
            JournalOpen(self.database, self.signals),
            self.clients,
            reshower,
            self.outbox,
            Clock(at),
            destinations=FixedDestinations([OWNER]),
            channel="telegram",
            install_approval=InstallApprovalPresenter(),
            approval_expiry_h=InfrastructureSettings().approval_expiry_h,
        )
        goal = goal_command(
            self.database, self.clients, self.outbox, Clock(at), FixedDestinations()
        )
        return Commands(
            self.outbox,
            Clock(at),
            status,
            opened,
            goal,
            channel="telegram",
            name_max_chars=COMMAND_NAME_MAX_CHARS,
        )

    def open(self, update: int = 700, at: datetime = NOW) -> None:
        item = Inbound(update, InboundKind.TEXT, 1001, 1001, "1001/90", text="/open")
        asyncio.run(self.commands(at).handle(item))

    def resolve_in_shikiru(self) -> None:
        record = self.client.view.escalation.model_copy(update={"status": "resolved"})
        self.client.view = self.client.view.model_copy(update={"escalation": record})

    def confirm_in_shikiru(self, at: datetime = NOW) -> None:
        self.ops.confirm_command("api", "12", at)

    def handle(
        self,
        update: int = 700,
        ref: str = "1001/42",
        data: str | None = None,
        text: str | None = None,
        at: datetime = NOW,
        *,
        install_replies: InstallReplyHandler | None = None,
    ) -> None:
        replies = Replies(
            self.log(),
            self.outbox,
            Clock(at),
            self.clients,
            channel="telegram",
            commands=self.commands(at),
            goal=goal_command(
                self.database, self.clients, self.outbox, Clock(at), FixedDestinations()
            ),
            install_replies=install_replies,
        )
        item = Inbound(
            update,
            InboundKind.TAP if text is None else InboundKind.REPLY,
            1001,
            1001,
            ref if text is None else "1001/90",
            reply_to_ref=ref if text else None,
            text=text,
            data=data,
        )
        asyncio.run(replies.handle(item))

    def install_replies(
        self,
        *,
        clock_at: datetime = NOW,
        message_at: datetime = NOW,
        origin_key: str | None = None,
    ) -> tuple[InstallApprovalReplies, int]:
        self.signals.classify(
            self.number,
            category=INFRASTRUCTURE,
            classified_by="reasoner",
            verdict=Verdict.HOST_TOOL,
            handler="infrastructure",
            now=NOW,
            note="model suggestion",
            install_proposal=InstallProposal(
                tool="ffmpeg", manager="brew", package="ffmpeg", why="needed by the build"
            ),
        )
        stored = self.signals.get(self.number)
        assert stored is not None
        signal = stored.to_signal()
        payload = InstallOpPayload(
            number=self.number,
            escalation_id="E-1",
            tool="ffmpeg",
            manager="brew",
            package="ffmpeg",
            why="needed by the build",
            argv=("brew", "install", "ffmpeg"),
        )
        operation = self.ops.intend_install(f"install:{signal.id}", signal, payload, NOW)
        approval_key = origin_key or f"install:{operation.id}"
        self.origin(
            "1001/43",
            Purpose.APPROVAL,
            (install_data(self.number, operation.id), decline_data(self.number, operation.id)),
            key=approval_key,
            at=message_at,
        )
        forwarder = ProductForwarder(
            MESSAGE_TITLE_MAX_CHARS,
            self.clients,
            self.outbox,
            Clock(clock_at),
            FixedDestinations([OWNER]),
            button_label_max_chars=BUTTON_LABEL_MAX_CHARS,
        )
        replies = InstallApprovalReplies(
            self.ops,
            self.clients,
            forwarder,
            self.outbox,
            Clock(clock_at),
            approval_expiry_h=InfrastructureSettings().approval_expiry_h,
            runner=self.runner,
        )
        return replies, operation.id


@pytest.fixture
def h() -> Generator[Harness]:
    harness = Harness()
    yield harness
    harness.database.close()


def test_text_reply_to_a_question_answers_and_queues_no_command_reply(h: Harness) -> None:
    h.handle(text="Use the other approach")

    assert h.client.calls
    assert h.outbox.pending() == []


def test_install_tap_approves_a_stored_op_once(h: Harness) -> None:
    handler, op_id = h.install_replies()
    data = install_data(h.number, op_id)

    h.handle(ref="1001/43", data=data, install_replies=handler)
    h.handle(ref="1001/43", data=data, install_replies=handler)

    operation = h.ops.get(op_id)
    assert operation is not None
    assert operation.state is OpState.APPROVED
    assert h.runner.started == [op_id]
    assert h.outbox.pending() == []


def test_expiry_uses_original_op_creation_after_a_resent_message(h: Harness) -> None:
    expiry = InfrastructureSettings().approval_expiry_h
    resent_at = NOW + timedelta(hours=expiry - 1)
    now = NOW + timedelta(hours=expiry + 1)
    handler, op_id = h.install_replies(clock_at=now, message_at=resent_at)
    data = decline_data(h.number, op_id)

    h.handle(ref="1001/43", data=data, at=now, install_replies=handler)
    h.handle(ref="1001/43", data=data, at=now, install_replies=handler)

    operation = h.ops.get(op_id)
    assert operation is not None
    assert operation.state is OpState.EXPIRED
    [pending] = h.outbox.pending()
    assert "expired" in pending.message.text
    assert h.client.calls == []


def test_resolved_elsewhere_invalidates_even_before_the_event_is_journaled(h: Harness) -> None:
    handler, op_id = h.install_replies()
    h.resolve_in_shikiru()

    h.handle(ref="1001/43", data=install_data(h.number, op_id), install_replies=handler)

    operation = h.ops.get(op_id)
    signal = h.signals.get(h.number)
    assert operation is not None
    assert signal is not None
    assert operation.state is OpState.VOID
    assert signal.state is SignalState.NEW
    [pending] = h.outbox.pending()
    assert "no longer open" in pending.message.text


def test_decline_is_durable_and_product_forward_is_replay_safe(h: Harness) -> None:
    handler, op_id = h.install_replies()
    data = decline_data(h.number, op_id)

    h.handle(ref="1001/43", data=data, install_replies=handler)
    h.handle(ref="1001/43", data=data, install_replies=handler)

    operation = h.ops.get(op_id)
    forwards = [
        message
        for message in h.messages.for_signal(h.number)
        if message.body is not None and "declined the install approval" in message.body.text
    ]
    assert operation is not None
    assert operation.state is OpState.DECLINED
    assert h.runner.started == []
    assert len(forwards) == 1
    assert forwards[0].purpose is Purpose.QUESTION
    assert forwards[0].body is not None
    assert "Model note: model suggestion" in forwards[0].body.text


def test_open_replays_the_same_intended_install_op_with_new_update_keys(h: Harness) -> None:
    handler, op_id = h.install_replies()
    stored = h.ops.install_op(op_id)
    assert stored is not None

    h.open(update=700)
    [first] = h.outbox.pending()
    h.open(update=701)
    h.open(update=700)
    copies = h.outbox.pending()

    assert len(copies) == 2
    assert copies[0].message.text == copies[1].message.text
    assert copies[0].message.buttons == copies[1].message.buttons
    assert f"Command: {' '.join(stored.payload.argv)}" in first.message.text
    assert copies[0].message.buttons[0].data == install_data(h.number, op_id)
    assert h.ops.install_op(op_id) == stored
    h.outbox.confirm(copies[1].message_id, "1001/44")
    h.handle(ref="1001/44", data=install_data(h.number, op_id), install_replies=handler)
    decided = h.ops.install_op(op_id)
    assert decided is not None
    assert decided.state == OpState.APPROVED
    assert decided.payload == stored.payload


def test_open_does_not_renew_an_expired_install_approval(h: Harness) -> None:
    h.install_replies()
    expiry = InfrastructureSettings().approval_expiry_h

    h.open(update=701, at=NOW + timedelta(hours=expiry))

    assert all("Install approval" not in message.message.text for message in h.outbox.pending())


def test_open_does_not_reoffer_an_approved_install(h: Harness) -> None:
    _, op_id = h.install_replies()
    assert h.ops.approve_install(op_id, h.number, NOW)

    h.open(update=701)

    assert all("Install approval" not in message.message.text for message in h.outbox.pending())


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
        label="retry",
    )
    h.log().intend_resolve(700, payload, NOW)
    h.handle(data="a:1:1")
    assert h.client.calls == []
    assert other.calls == [("E-2", "retry", None)]


@pytest.mark.parametrize(("data", "text"), [("a:1:2", None), (None, "Use the other approach")])
def test_later_answer_to_a_resolved_question_gets_the_recorded_choice_and_time(
    h: Harness, data: str | None, text: str | None
) -> None:
    h.handle(data="a:1:1", at=ANSWERED_AT)
    h.resolve_in_shikiru()
    h.confirm_in_shikiru(ANSWERED_AT)
    h.handle(update=701, data=data, text=text, at=LATER_AT)
    h.handle(update=701, data=data, text=text, at=LATER_AT)
    (notice,) = h.outbox.pending()
    assert "Keep existing behavior" in notice.message.text
    assert "12:03 UTC" in notice.message.text
    assert "13:30" not in notice.message.text
    assert h.client.calls == [
        ("E-1", "answer", "Keep existing behavior (Preserve the current call pattern.)")
    ]


class EditChannel:
    async def send(self, destination: Destination, message: Message) -> str:
        raise NotImplementedError

    async def edit(self, message_ref: str, message: Message) -> None:
        pass

    async def delete(self, message_ref: str) -> None:
        raise NotImplementedError


def test_a_tap_processed_after_the_buttons_were_removed_still_gets_the_recorded_resolution(
    h: Harness,
) -> None:
    h.handle(data="a:1:1", at=ANSWERED_AT)
    h.resolve_in_shikiru()
    h.confirm_in_shikiru(ANSWERED_AT)
    editor = CopyEditor(
        JournalFinalEdits(h.database, h.log()), EditChannel(), Clock(LATER_AT), interval_s=1
    )
    asyncio.run(editor.tick())
    h.handle(update=701, data="a:1:2", at=LATER_AT)
    (notice,) = h.outbox.pending()
    assert "Keep existing behavior" in notice.message.text


def test_recorded_answer_longer_than_a_message_is_cut_so_its_notice_still_goes(h: Harness) -> None:
    h.handle(text="A" * MESSAGE_TEXT_LIMIT, at=ANSWERED_AT)
    h.resolve_in_shikiru()
    h.confirm_in_shikiru(ANSWERED_AT)
    h.handle(update=701, text="Another answer", at=LATER_AT)
    (notice,) = h.outbox.pending()
    assert len(notice.message.text) <= MESSAGE_TEXT_LIMIT
    assert notice.message.text.startswith(f"S{h.number} · Already resolved: AAA")
    assert notice.message.text.endswith("… at 12:03 UTC")


def test_later_answer_to_a_question_resolved_elsewhere_gets_no_recorded_answer(h: Harness) -> None:
    h.resolve_in_shikiru()
    h.handle(data="a:1:2")
    assert h.outbox.pending() == []
    assert h.client.calls == []


def test_rejected_resolution_is_not_recorded_as_the_resolution(h: Harness) -> None:
    h.client.reject = True
    h.handle(data="a:1:1")
    (rejection,) = h.outbox.pending()
    h.resolve_in_shikiru()
    h.handle(update=701, data="a:1:2")
    assert h.outbox.pending() == [rejection]
