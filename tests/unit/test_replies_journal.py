import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from shikiri_yaku.domain.callback import decode_callback
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.journal.database import Database
from shikiri_yaku.journal.messages import MessageStore
from shikiri_yaku.journal.ops import OpState, OpStore
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.journal.signals import NewSignal, SignalStore
from shikiri_yaku.ports import (
    Ack,
    Button,
    EscalationView,
    Message,
    ResolvePayload,
    escalation_choices,
)

NOW: Final = datetime(2026, 10, 28, tzinfo=UTC)
PAYLOAD: Final = ResolvePayload(
    number=1,
    project="api",
    task_id="T-1",
    reason="decision_required",
    escalation_id="E-1",
    kind="answer",
    text="Keep (preserves behavior)",
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
