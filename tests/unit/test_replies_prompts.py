from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.secrets import secret_form
from shikiri_yaku.extensions.product.message import escalation_message
from shikiri_yaku.journal import NewSignal, OpState, SignalState
from shikiri_yaku.ports import MESSAGE_TEXT_LIMIT
from shikiri_yaku.settings import BUTTON_LABEL_MAX_CHARS
from tests.support.secrets import SECRETS
from tests.unit.test_replies import ANSWERED_AT, LATER_AT, NOW, Harness

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture
def h() -> Generator[Harness]:
    harness = Harness()
    yield harness
    harness.database.close()


@pytest.mark.parametrize("option", [1, 3])
def test_input_requests_are_durable_question_messages_matched_by_reply_reference(
    h: Harness, option: int
) -> None:
    value = h.client.view.agent_options[0].model_copy(update={"needs_input": True})
    h.client.view = h.client.view.model_copy(
        update={"agent_options": [value, h.client.view.agent_options[1]]}
    )
    h.handle(data=f"a:1:{option}")
    h.handle(data=f"a:1:{option}")
    (pending,) = h.outbox.pending()
    assert pending.destination.chat == "1001"
    assert h.client.calls == []
    h.outbox.confirm(pending.message_id, "1001/43")
    h.handle(update=701, ref="1001/43", text="My complete answer")
    assert h.client.calls == [("E-1", "answer", "My complete answer")]


@pytest.mark.parametrize("kind", ["accept", "cancel"])
def test_confirmation_survives_reconstruction_and_accepts_only_its_stored_button(
    h: Harness, kind: str
) -> None:
    value = h.client.view.resolutions[-1].model_copy(update={"kind": kind})
    h.client.view = h.client.view.model_copy(update={"agent_options": [], "resolutions": [value]})
    h.handle(data="a:1:1")
    h.handle(data="a:1:1")
    (pending,) = h.outbox.pending()
    (op,) = h.ops.non_final()
    assert op.state == OpState.INTENDED
    assert h.client.calls == []
    h.outbox.confirm(pending.message_id, "1001/43")
    h.handle(update=701, ref="1001/43", text="yes")
    h.handle(update=702, ref="1001/43", data="a:1:2")
    h.origin("1001/44", Purpose.APPROVAL, ("a:1:1",))
    h.handle(update=703, ref="1001/44", data="a:1:1")
    assert h.client.calls == []
    h.handle(update=704, ref="1001/43", data="a:1:1")
    h.handle(update=705, ref="1001/43", data="a:1:1")
    assert h.client.calls == [("E-1", kind, None)]
    (op,) = h.ops.non_final()
    assert op.state == OpState.ISSUED


def test_confirmation_does_not_resolve_a_closed_escalation(h: Harness) -> None:
    h.handle(data="a:1:4")
    (pending,) = h.outbox.pending()
    h.outbox.confirm(pending.message_id, "1001/43")
    record = h.client.view.escalation.model_copy(update={"status": "resolved"})
    h.client.view = h.client.view.model_copy(update={"escalation": record})
    h.handle(update=701, ref="1001/43", data="a:1:4")
    assert h.client.calls == []


def test_confirmation_after_the_operator_resolved_gets_the_recorded_choice(h: Harness) -> None:
    value = h.client.view.resolutions[-1].model_copy(update={"kind": "accept"})
    h.client.view = h.client.view.model_copy(update={"agent_options": [], "resolutions": [value]})
    h.handle(data="a:1:1")
    (pending,) = h.outbox.pending()
    h.outbox.confirm(pending.message_id, "1001/43")
    h.handle(update=701, ref="1001/43", data="a:1:1", at=ANSWERED_AT)
    h.resolve_in_shikiru()
    h.confirm_in_shikiru(ANSWERED_AT)
    h.handle(update=702, ref="1001/43", data="a:1:1", at=LATER_AT)
    (notice,) = h.outbox.pending()
    assert "accept" in notice.message.text
    assert "12:03 UTC" in notice.message.text
    assert h.client.calls == [("E-1", "accept", None)]


def test_details_queue_full_packet_in_parts_once_per_update(h: Harness) -> None:
    packet = "Q" * (MESSAGE_TEXT_LIMIT + 1)
    h.client.view = h.client.view.model_copy(update={"packet": packet})
    h.origin("1001/44", Purpose.QUESTION, ("d:1",))
    h.handle(ref="1001/44", data="d:1")
    h.handle(ref="1001/44", data="d:1")
    parts = h.outbox.pending()
    assert [len(part.message.text) for part in parts] == [MESSAGE_TEXT_LIMIT, 1]
    assert "".join(part.message.text for part in parts) == packet
    assert all(part.destination.chat == "1001" for part in parts)
    assert h.client.calls == []


def test_stop_reminders_on_information_closes_without_shikiru(h: Harness) -> None:
    h.number = h.signals.insert_if_absent(
        NewSignal("api:task_done:7", "api", "task_done", "task_done", "task done"),
        NOW,
    ).row_id
    h.signals.transition(h.number, SignalState.NEW, SignalState.FORWARDED, NOW)
    h.origin("1001/45", Purpose.NOTICE, (f"k:{h.number}",))
    h.handle(ref="1001/45", data=f"k:{h.number}")
    h.handle(ref="1001/45", data=f"k:{h.number}")
    stored = h.signals.get(h.number)
    assert stored is not None
    assert stored.state == SignalState.CLOSED
    assert h.client.calls == []
    assert h.ops.non_final() == []


@pytest.mark.parametrize(
    ("kind", "silenced"),
    [("pr_review", True), ("task_done", False)],
    ids=["standing notice silences and stays open", "information closes"],
)
def test_stop_reminders_silences_a_standing_notice_and_closes_information(
    h: Harness, kind: str, *, silenced: bool
) -> None:
    h.number = h.signals.insert_if_absent(
        NewSignal(f"api:{kind}:T-1", "api", kind, "x", "x", "T-1"), NOW
    ).row_id
    h.signals.transition(h.number, SignalState.NEW, SignalState.FORWARDED, NOW)
    h.origin("1001/45", Purpose.NOTICE, (f"k:{h.number}",), key=f"notice:{h.number}")
    h.handle(ref="1001/45", data=f"k:{h.number}")
    h.handle(ref="1001/45", data=f"k:{h.number}")
    stored = h.signals.get(h.number)
    assert stored is not None
    assert stored.state == (SignalState.FORWARDED if silenced else SignalState.CLOSED)
    rows = h.database.connection.execute("SELECT identity FROM ops")
    assert [row["identity"] for row in rows] == ([f"ack:{h.number}"] if silenced else [])
    assert h.client.calls == []


@pytest.mark.parametrize("secret", list(SECRETS.values()))
def test_secret_crossing_a_details_part_boundary_is_refused_before_splitting(
    h: Harness,
    secret: str,
) -> None:
    packet = " " * (MESSAGE_TEXT_LIMIT - 5) + secret
    h.client.view = h.client.view.model_copy(update={"packet": packet})
    h.origin("1001/44", Purpose.QUESTION, ("d:1",))
    h.handle(ref="1001/44", data="d:1")
    (notice,) = h.outbox.pending()
    assert secret not in notice.message.text
    assert all(
        message.body is None or secret not in message.body.text
        for message in h.messages.for_signal(h.number)
    )


@pytest.mark.parametrize("kind", ["accept", "cancel"])
def test_replayed_selection_cannot_issue_stored_confirmation_before_approval(
    h: Harness,
    kind: str,
) -> None:
    value = h.client.view.resolutions[-1].model_copy(update={"kind": kind})
    h.client.view = h.client.view.model_copy(
        update={"resolutions": [h.client.view.resolutions[0], value]}
    )
    h.handle(data="a:1:4")
    (pending,) = h.outbox.pending()
    retry = value.model_copy(update={"kind": "retry"})
    h.client.view = h.client.view.model_copy(
        update={"resolutions": [h.client.view.resolutions[0], retry]}
    )
    h.handle(data="a:1:4")
    assert len(h.client.calls) == 0
    (op,) = h.ops.non_final()
    assert op.state == OpState.INTENDED
    h.outbox.confirm(pending.message_id, "1001/43")
    h.handle(update=701, ref="1001/43", text="yes")
    h.handle(update=702, ref="1001/43", data="a:1:3")
    assert len(h.client.calls) == 0
    h.handle(update=703, ref="1001/43", data="a:1:4")
    h.handle(data="a:1:4")
    assert h.client.calls == [("E-1", kind, None)]


@pytest.mark.parametrize("form", list(SECRETS))
def test_secret_in_an_option_label_is_refused_before_journaling(h: Harness, form: str) -> None:
    # keep: a label is on its button, so it reaches the PO even when the text around it is cut
    option = h.client.view.agent_options[0]
    hidden = option.model_copy(update={"label": SECRETS[form]})
    h.client.view = h.client.view.model_copy(
        update={"agent_options": [hidden, h.client.view.agent_options[1]]}
    )
    stored_signal = h.signals.get(h.number)
    assert stored_signal is not None
    rendered = escalation_message(
        h.number,
        stored_signal.to_signal(),
        h.client.view,
        title="A task",
        in_topic=False,
        note="",
        suggestion="",
        label_max_chars=BUTTON_LABEL_MAX_CHARS,
    )
    assert secret_form(rendered.texts()) == form
    h.messages.record_outbound(
        signal_number=h.number,
        channel="telegram",
        chat="1001",
        purpose=Purpose.QUESTION,
        body=rendered,
        notice_key=None,
        now=NOW,
    )
    assert secret_form(h.database.connection.iterdump()) is None
    (notice,) = h.outbox.pending()
    assert secret_form(notice.message.texts()) is None


@pytest.mark.parametrize("form", list(SECRETS))
def test_truncated_rendered_option_refuses_secret_before_journaling_or_resolving(
    h: Harness,
    form: str,
) -> None:
    option = h.client.view.agent_options[0]
    hidden = option.model_copy(update={"consequences": SECRETS[form]})
    h.client.view = h.client.view.model_copy(
        update={
            "question": "Q" * MESSAGE_TEXT_LIMIT,
            "agent_options": [hidden, h.client.view.agent_options[1]],
        }
    )
    stored_signal = h.signals.get(h.number)
    assert stored_signal is not None
    rendered = escalation_message(
        h.number,
        stored_signal.to_signal(),
        h.client.view,
        title="A task",
        in_topic=False,
        note="",
        suggestion="",
        label_max_chars=BUTTON_LABEL_MAX_CHARS,
    )
    assert secret_form(rendered.texts()) is None
    row = h.messages.record_outbound(
        signal_number=h.number,
        channel="telegram",
        chat="1001",
        purpose=Purpose.QUESTION,
        body=rendered,
        notice_key=None,
        now=NOW,
    )
    h.messages.confirm_delivery(row.row_id, "1001/46")
    h.handle(ref="1001/46", data="a:1:1")
    h.handle(ref="1001/46", data="a:1:1")
    assert len(h.client.calls) == 0
    op = h.ops.get(1)
    assert op is not None
    assert op.state == OpState.FAILED
    assert json.loads(op.payload or "null") == {"refusal": form}
    assert secret_form(h.database.connection.iterdump()) is None
    (notice,) = h.outbox.pending()
    assert notice.message.buttons == ()
    assert secret_form(notice.message.texts()) is None
    h.client.view = h.client.view.model_copy(update={"agent_options": [option]})
    h.handle(ref="1001/46", data="a:1:1")
    assert len(h.client.calls) == 0
