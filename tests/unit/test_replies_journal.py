from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from pydantic import ValidationError

from shikiri_yaku.domain.callback import decode_callback
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.journal.database import Database
from shikiri_yaku.journal.messages import MessageStore
from shikiri_yaku.journal.ops import NewOp, OpState, OpStore
from shikiri_yaku.journal.reconcile import JournalReconcile
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.journal.signals import NewSignal, SignalState, SignalStore
from shikiri_yaku.ports import (
    Ack,
    Button,
    EscalationView,
    Message,
    RecordedResolution,
    ResolvePayload,
    escalation_choices,
)

if TYPE_CHECKING:
    from collections.abc import Generator

NOW: Final = datetime(2026, 10, 28, tzinfo=UTC)
LATER: Final = datetime(2026, 10, 28, 12, 3, tzinfo=UTC)
PAYLOAD: Final = ResolvePayload(
    number=1,
    project="api",
    task_id="T-1",
    reason="decision_required",
    escalation_id="E-1",
    kind="answer",
    text="Keep (preserves behavior)",
    label="Keep",
)


@pytest.mark.parametrize(
    ("data", "expected"), [("a:41:2", ("a", 41, 2)), ("d:41", ("d", 41)), ("k:41", ("k", 41))]
)
def test_callback_verbs_decode_separately(data: str, expected: tuple[str | int, ...]) -> None:
    assert decode_callback(data) == expected


@pytest.mark.parametrize(
    "data", ["a:41", "d:41:2", "k:41:2", "x:41", "a:0:1", "a:41:-1", "d:no", "a:41:1:2"]
)
def test_invalid_callback_is_refused(data: str) -> None:
    with pytest.raises(ValidationError):
        decode_callback(data)


def test_choices_share_numbers_including_unknown_resolution_gaps() -> None:
    fixture = Path(__file__).parents[1] / "fixtures/shikiru/get_escalation.json"
    view = EscalationView.model_validate(json.loads(fixture.read_text())["result"])
    unknown = view.resolutions[0].model_copy(update={"kind": "future"})
    view = view.model_copy(update={"resolutions": [unknown, *view.resolutions]})
    choices = escalation_choices(view)
    assert [choice.number for choice in choices] == [1, 2, 4, 5]
    assert [choice.value for choice in choices] == [*view.agent_options, *view.resolutions[1:]]


@pytest.mark.parametrize(
    ("kind", "state"),
    [("pr_review", SignalState.FORWARDED), ("health", SignalState.CLOSED)],
    ids=["reminded kind stays open", "other kind closes"],
)
def test_acknowledging_a_notice_keeps_a_reminded_kind_open_and_closes_the_others(
    kind: str, state: SignalState, memory: Database
) -> None:
    database = memory
    signals = SignalStore(database)
    number = signals.insert_if_absent(
        NewSignal(f"api:{kind}:1", "api", kind, "x", "x", "T-1"), NOW
    ).row_id
    signals.classify(
        number, category="product", classified_by="rule", verdict=None, handler="p", now=NOW
    )
    signals.transition(number, SignalState.NEW, SignalState.HANDLING, NOW)
    signals.transition(number, SignalState.HANDLING, SignalState.FORWARDED, NOW)

    JournalReplies(
        database, signals, OpStore(database), reminded_kinds=frozenset({"pr_review"})
    ).acknowledge_notice(number, NOW)

    stored = signals.get(number)
    assert stored is not None
    assert stored.state is state


@pytest.fixture
def memory() -> Generator[Database]:
    database = Database.open(":memory:")
    yield database
    database.close()


class _Reporter:
    def report(self, line: str) -> None:
        pytest.fail(line)


@pytest.mark.parametrize("purpose", list(Purpose))
def test_origin_binds_only_delivered_outbound_copy_to_channel_and_purpose(purpose: Purpose) -> None:
    database = Database.open(":memory:")
    signals = SignalStore(database)
    number = signals.insert_if_absent(
        NewSignal(
            "api:escalation:E-1", "api", "escalation", "decision_required", "question", "T-1", "E-1"
        ),
        NOW,
    ).row_id
    messages = MessageStore(database, _Reporter())
    body = Message("question", (Button("Retry", f"a:{number}:1"),))
    stored = messages.record_outbound(
        signal_number=number,
        channel="telegram",
        chat="1001",
        purpose=purpose,
        body=body,
        notice_key=None,
        now=NOW,
    )
    replies = JournalReplies(database, signals, OpStore(database))
    assert replies.origin("telegram", "1001/42") is None
    messages.confirm_delivery(stored.row_id, "1001/42")
    origin = replies.origin("telegram", "1001/42")
    assert origin is not None
    assert (origin.number, origin.signal.escalation_id, origin.purpose, origin.message) == (
        number,
        "E-1",
        purpose,
        body,
    )
    assert replies.origin("other", "1001/42") is None
    assert replies.origin("telegram", "1002/42") is None
    database.close()


@pytest.mark.parametrize("status", ["done", "queued"])
def test_update_identity_stores_first_payload_and_claims_once_across_reconstruction(
    status: str,
) -> None:
    database = Database.open(":memory:")
    ops = OpStore(database)
    replies = JournalReplies(database, SignalStore(database), ops)
    op_id = replies.intend_resolve(700, PAYLOAD, NOW)
    assert op_id is not None
    changed = PAYLOAD.model_copy(update={"text": "a different answer"})
    assert replies.intend_resolve(700, changed, NOW) == op_id
    assert replies.claim_resolve(op_id, NOW) == PAYLOAD
    restarted = JournalReplies(database, SignalStore(database), OpStore(database))
    assert restarted.claim_resolve(op_id, NOW) is None
    ack = Ack(id=12, status=status, result=None, message=None)
    restarted.acknowledge_resolve(op_id, ack, NOW)
    stored = ops.get(op_id)
    assert stored is not None
    assert (stored.state, stored.command_id, stored.acknowledgement) == (OpState.ISSUED, "12", ack)
    assert ops.non_final() == [stored]
    assert restarted.claim_resolve(op_id, NOW) is None
    database.close()


@pytest.mark.parametrize("kind", ["accept", "cancel"])
def test_confirmation_requires_durable_approval_of_the_same_signal(kind: str) -> None:
    database = Database.open(":memory:")
    replies = JournalReplies(database, SignalStore(database), OpStore(database))
    payload = PAYLOAD.model_copy(update={"kind": kind, "text": None})
    op_id = replies.intend_resolve(700, payload, NOW)
    assert op_id is not None
    assert replies.claim_resolve(op_id, NOW) is None
    assert not replies.approve_resolve(op_id, payload.number + 1, NOW)
    assert replies.claim_resolve(op_id, NOW) is None
    assert replies.approve_resolve(op_id, payload.number, NOW)
    restarted = JournalReplies(database, SignalStore(database), OpStore(database))
    assert restarted.claim_resolve(op_id, NOW) == payload
    assert restarted.claim_resolve(op_id, NOW) is None
    database.close()


@pytest.mark.parametrize(
    ("state", "recorded"),
    [
        (OpState.INTENDED, False),
        (OpState.APPROVED, False),
        (OpState.ISSUED, False),
        (OpState.CONFIRMED, True),
        (OpState.FAILED, False),
        (OpState.UNKNOWN, False),
    ],
)
def test_only_a_resolve_shikiru_confirmed_is_the_recorded_resolution(
    state: OpState, *, recorded: bool
) -> None:
    database = Database.open(":memory:")
    ops = OpStore(database)
    replies = JournalReplies(database, SignalStore(database), ops)
    op_id = replies.intend_resolve(700, PAYLOAD, NOW)
    assert op_id is not None
    assert ops.transition(op_id, OpState.INTENDED, state, LATER)
    expected = RecordedResolution("Keep", LATER) if recorded else None
    assert replies.recorded_resolution(PAYLOAD.number) == expected
    database.close()


def test_recorded_resolution_is_never_another_signals() -> None:
    database = Database.open(":memory:")
    ops = OpStore(database)
    replies = JournalReplies(database, SignalStore(database), ops)
    other = replies.intend_resolve(700, PAYLOAD.model_copy(update={"number": 2}), NOW)
    assert other is not None
    assert ops.transition(other, OpState.INTENDED, OpState.CONFIRMED, LATER)
    assert replies.recorded_resolution(PAYLOAD.number) is None
    database.close()


def test_an_unconfirmed_attempt_never_displaces_the_confirmed_winner() -> None:
    database = Database.open(":memory:")
    ops = OpStore(database)
    replies = JournalReplies(database, SignalStore(database), ops)
    winner = replies.intend_resolve(700, PAYLOAD, NOW)
    loser = replies.intend_resolve(701, PAYLOAD.model_copy(update={"label": "Change"}), NOW)
    assert winner is not None
    assert loser is not None
    for op_id, ack_id in ((winner, 11), (loser, 12)):
        assert replies.claim_resolve(op_id, NOW) is not None
        ack = Ack(id=ack_id, status="queued", result=None, message=None)
        replies.acknowledge_resolve(op_id, ack, NOW)
    assert ops.confirm_command("api", "11", LATER)
    assert replies.recorded_resolution(PAYLOAD.number) == RecordedResolution("Keep", LATER)
    database.close()


def test_rows_journaled_before_the_label_stay_claimable_and_recorded_by_their_text() -> None:
    database = Database.open(":memory:")
    signals = SignalStore(database)
    ops = OpStore(database)
    replies = JournalReplies(database, signals, ops)
    number = signals.insert_if_absent(
        NewSignal(
            "api:escalation:E-1", "api", "escalation", "decision_required", "question", "T-1", "E-1"
        ),
        NOW,
    ).row_id
    legacy = PAYLOAD.model_copy(update={"number": number}).model_dump_json(exclude={"label"})
    op_id = ops.intend(
        NewOp(
            identity="telegram:resolve:700",
            kind="resolve",
            project="api",
            task_id="T-1",
            reason="decision_required",
            payload=legacy,
        ),
        NOW,
    ).row_id
    assert replies.claim_resolve(op_id, NOW) == ResolvePayload.model_validate_json(legacy)
    (unsettled,) = JournalReconcile(database, signals, ops).unsettled()
    assert unsettled.op_id == op_id
    replies.acknowledge_resolve(op_id, Ack(id=12, status="queued", result=None, message=None), NOW)
    assert ops.confirm_command("api", "12", LATER)
    assert replies.recorded_resolution(number) == RecordedResolution(
        "Keep (preserves behavior)", LATER
    )
    database.close()
