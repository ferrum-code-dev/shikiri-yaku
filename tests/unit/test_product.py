from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.adapters.telegram_topics import TOPIC_CLOSED, TOPIC_OPEN, TelegramDestinations
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.domain.outcome import Forward
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.extensions.product.forwarder import ProductForwarder
from shikiri_yaku.extensions.product.handler import NOTHING_TRIED, ProductHandler
from shikiri_yaku.extensions.product.message import (
    CUT_MARK,
    RECOMMENDED,
    escalation_message,
    information_message,
)
from shikiri_yaku.journal import Database
from shikiri_yaku.journal.topics import TopicStore
from shikiri_yaku.ports import (
    CALLBACK_DATA_LIMIT,
    MESSAGE_TEXT_LIMIT,
    Button,
    EscalationView,
    Message,
    Pending,
    ShikiruClient,
    ShikiruLinkDownError,
    TaskDetail,
)
from shikiri_yaku.settings import BUTTON_LABEL_MAX_CHARS, MESSAGE_TITLE_MAX_CHARS
from tests.support.destinations import FixedDestinations

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

_FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "shikiru"
NUMBER: Final = 41
TITLE: Final = "Pick the layout"
NOTE: Final = "nothing, this is a product question"
SUGGESTION: Final = "add it to pyproject.toml"
ESCALATION: Final = Signal(
    "api", Kind.ESCALATION, "E-1", "decision_required", "A product decision", "T-1", "E-1"
)
INFORMATION: Final = Signal("api", Kind.TASK_DONE, "event-1", "", "Task finished", "T-1")
LONG_TITLE: Final = "W" * 100
CUT_TITLE: Final = "W" * (MESSAGE_TITLE_MAX_CHARS - len(CUT_MARK)) + CUT_MARK


def _fixture_view() -> EscalationView:
    payload = json.loads((_FIXTURES / "get_escalation.json").read_text(encoding="utf-8"))
    return EscalationView.model_validate(payload["result"])


def _view(
    question: str = "Which?",
    options: list[dict[str, object]] | None = None,
    resolutions: list[dict[str, object]] | None = None,
) -> EscalationView:
    return EscalationView.model_validate(
        {
            "escalation": {
                "id": "E-1",
                "task_id": "T-1",
                "reason": "decision_required",
                "headline": "A product decision is needed",
                "status": "open",
                "resolution": None,
            },
            "packet": None,
            "question": question,
            "agent_options": options or [],
            "resolutions": resolutions or [],
        }
    )


def _option(
    label: str, *, needs_input: bool = False, recommended: bool = False
) -> dict[str, object]:
    return {
        "label": label,
        "consequences": "",
        "recommended": recommended,
        "needs_input": needs_input,
    }


def _resolution(kind: str, *, recommended: bool = False) -> dict[str, object]:
    return {"kind": kind, "consequences": "", "recommended": recommended}


class _Client:
    def __init__(
        self,
        view: EscalationView | None = None,
        error: Exception | None = None,
        *,
        title: str = TITLE,
        task_error: Exception | None = None,
    ) -> None:
        self.view = view
        self.error = error
        self.title = title
        self.task_error = task_error
        self.asked: list[str] = []

    async def get_escalation(self, escalation_id: str) -> EscalationView | None:
        self.asked.append(escalation_id)
        if self.error is not None:
            raise self.error
        return self.view

    async def get_task(self, task_id: str) -> TaskDetail | None:
        if self.task_error is not None:
            raise self.task_error
        return TaskDetail.model_validate(
            {
                "task": {
                    "id": task_id,
                    "parent_id": None,
                    "status": "in_progress",
                    "title": self.title,
                }
            }
        )


class _Outbox:
    def __init__(self) -> None:
        self.queued: list[tuple[Purpose, tuple[Destination, ...], Message, str]] = []

    def queue(
        self,
        number: int | None,
        purpose: Purpose,
        destinations: Sequence[Destination],
        message: Message,
        *,
        key: str,
        now: datetime,
    ) -> None:
        del number, now
        self.queued.append((purpose, tuple(destinations), message, key))

    def command_reply_queued(self, key: str) -> bool:
        del key
        return False

    def queue_command_reply(
        self,
        destinations: Sequence[Destination],
        parts: Sequence[Message],
        *,
        key: str,
        now: datetime,
    ) -> None:
        del destinations, parts, key, now
        raise NotImplementedError

    def pending(self) -> list[Pending]:
        raise NotImplementedError

    def confirm(self, message_id: int, message_ref: str) -> None:
        raise NotImplementedError

    def refuse(self, message_id: int, reason: str, *, now: datetime) -> None:
        raise NotImplementedError


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 10, 28, tzinfo=UTC)

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


def _forwarder(client: _Client, outbox: _Outbox, *chats: str) -> ProductForwarder:
    destinations = [Destination("telegram", chat) for chat in chats]
    return ProductForwarder(
        MESSAGE_TITLE_MAX_CHARS,
        {"api": cast("ShikiruClient", client)},
        outbox,
        _Clock(),
        FixedDestinations(destinations),
        button_label_max_chars=BUTTON_LABEL_MAX_CHARS,
    )


@pytest.fixture
def topics() -> Generator[TopicStore]:
    database = Database.open(":memory:")
    store = TopicStore(database)
    store.save("api", 73, TOPIC_OPEN)
    store.save(None, 74, TOPIC_OPEN)
    yield store
    database.close()


def test_fixture_view_message_has_question_options_and_one_recommended_mark() -> None:
    message = escalation_message(
        NUMBER,
        ESCALATION,
        _fixture_view(),
        title=TITLE,
        in_topic=False,
        note=NOTE,
        suggestion="",
        label_max_chars=BUTTON_LABEL_MAX_CHARS,
    )
    assert "Which behavior should remain?" in message.text
    assert "Keep existing behavior" in message.text
    assert "Change behavior" in message.text
    assert "Preserve the current call pattern." in message.text
    assert message.text.count(RECOMMENDED) == 1
    assert "## " not in message.text
    assert "`" not in message.text


def test_group_escalations_use_the_component_topic_and_other_signals_use_general() -> None:
    database = Database.open(":memory:")
    try:
        topics = TopicStore(database)
        topics.save("api", 73, TOPIC_OPEN)
        topics.save("general", 75, TOPIC_OPEN)
        topics.save(None, 74, TOPIC_OPEN)
        destinations = TelegramDestinations(topics, owners=(1001,), group_id=-1009, reconciler=None)
        outbox = _Outbox()
        forwarder = ProductForwarder(
            MESSAGE_TITLE_MAX_CHARS,
            {"api": cast("ShikiruClient", _Client(_fixture_view()))},
            outbox,
            _Clock(),
            destinations,
            button_label_max_chars=BUTTON_LABEL_MAX_CHARS,
        )

        asyncio.run(forwarder.forward(NUMBER, ESCALATION, NOTE, ""))
        notice = Signal("api", Kind.TASK_DONE, "event-1", "", "Task finished")
        asyncio.run(forwarder.forward(NUMBER + 1, notice, "", ""))

        assert outbox.queued[0][1] == (Destination("telegram", "-1009", 73),)
        assert outbox.queued[1][1] == (Destination("telegram", "-1009", 74),)
        private = TelegramDestinations(topics, owners=(1001,), group_id=None, reconciler=None)
        assert private.for_signal("api", Kind.ESCALATION) == [Destination("telegram", "1001")]
        assert private.for_signal("api", Kind.TASK_DONE) == [Destination("telegram", "1001")]
        assert destinations.for_signal("general", Kind.ESCALATION) == [
            Destination("telegram", "-1009", 75)
        ]
    finally:
        database.close()


def test_only_an_open_topic_names_its_component() -> None:
    database = Database.open(":memory:")
    try:
        topics = TopicStore(database)
        topics.save("api", 73, TOPIC_OPEN)
        topics.save("web", 74, TOPIC_CLOSED)
        destinations = TelegramDestinations(topics, owners=(1001,), group_id=-1009, reconciler=None)

        assert destinations.component_of(73) == "api"
        assert destinations.component_of(74) is None
        assert destinations.component_of(99) is None
    finally:
        database.close()


def test_fixture_view_offers_options_then_resolutions_with_numbered_callback_data() -> None:
    message = escalation_message(
        NUMBER,
        ESCALATION,
        _fixture_view(),
        title=TITLE,
        in_topic=False,
        note=NOTE,
        suggestion="",
        label_max_chars=BUTTON_LABEL_MAX_CHARS,
    )
    assert message.buttons == (
        Button("★ 1. Keep existing behavior", "a:41:1"),
        Button("2. Change behavior", "a:41:2"),
        Button("Reply with text", "a:41:3"),
        Button("Cancel task", "a:41:4"),
    )


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        (("answer",), [Button("Reply with text", "a:41:1")]),
        (("retry",), [Button("Retry", "a:41:1")]),
        (("accept",), [Button("Accept", "a:41:1")]),
        (("cancel",), [Button("Cancel task", "a:41:1")]),
        (
            ("retry", "cancel"),
            [Button("Retry", "a:41:1"), Button("Cancel task", "a:41:2")],
        ),
        (
            ("accept", "cancel"),
            [Button("Accept", "a:41:1"), Button("Cancel task", "a:41:2")],
        ),
    ],
)
def test_escalation_without_agent_options_offers_only_its_resolutions(
    kinds: tuple[str, ...], expected: list[Button]
) -> None:
    view = _view(resolutions=[_resolution(kind) for kind in kinds])
    message = escalation_message(
        NUMBER,
        ESCALATION,
        view,
        title=TITLE,
        in_topic=False,
        note=NOTE,
        suggestion="",
        label_max_chars=BUTTON_LABEL_MAX_CHARS,
    )
    assert list(message.buttons) == expected


@pytest.mark.parametrize("needs_input", [False, True])
def test_option_that_needs_input_uses_the_same_callback_as_the_others(*, needs_input: bool) -> None:
    view = _view(options=[_option("Keep", needs_input=needs_input)])
    message = escalation_message(
        NUMBER,
        ESCALATION,
        view,
        title=TITLE,
        in_topic=False,
        note=NOTE,
        suggestion="",
        label_max_chars=BUTTON_LABEL_MAX_CHARS,
    )
    assert message.buttons == (Button("1. Keep", "a:41:1"),)


def test_resolution_kind_the_operator_does_not_know_is_not_offered_and_keeps_its_number() -> None:
    view = _view(resolutions=[_resolution("skip"), _resolution("retry")])
    message = escalation_message(
        NUMBER,
        ESCALATION,
        view,
        title=TITLE,
        in_topic=False,
        note=NOTE,
        suggestion="",
        label_max_chars=BUTTON_LABEL_MAX_CHARS,
    )
    assert message.buttons == (Button("Retry", "a:41:2"),)


@pytest.mark.parametrize(
    ("kind", "label"),
    [
        ("answer", "Reply with text"),
        ("retry", "Retry"),
        ("accept", "Accept"),
        ("cancel", "Cancel task"),
    ],
)
def test_recommended_resolution_keeps_its_button_label_and_is_marked_on_its_line(
    kind: str, label: str
) -> None:
    view = _view(resolutions=[_resolution(kind, recommended=True)])
    message = escalation_message(
        NUMBER,
        ESCALATION,
        view,
        title=TITLE,
        in_topic=False,
        note=NOTE,
        suggestion="",
        label_max_chars=BUTTON_LABEL_MAX_CHARS,
    )
    assert message.buttons == (Button(label, "a:41:1"),)
    assert f"{RECOMMENDED} {label}" in message.text
    assert message.text.count(RECOMMENDED) == 1


@pytest.mark.parametrize(
    ("kinds", "hinted"),
    [(("retry", "cancel"), True), (("cancel",), False)],
)
def test_reason_hint_is_shown_only_where_retry_is_offered(
    *, kinds: tuple[str, ...], hinted: bool
) -> None:
    signal = Signal("api", Kind.ESCALATION, "E-1", "agent_auth", "text", "T-1", "E-1")
    view = _view(resolutions=[_resolution(kind) for kind in kinds])
    message = escalation_message(
        NUMBER,
        signal,
        view,
        title=TITLE,
        in_topic=False,
        note=NOTE,
        suggestion="",
        label_max_chars=BUTTON_LABEL_MAX_CHARS,
    )
    assert ("Log in to the agent again" in message.text) is hinted


# keep: the default limit of 30 cuts this label to 29 characters and the mark
LONG_LABEL: Final = "Ship the export for new invoices now and the history later"
LONG_LABEL_CUT: Final = "★ 1. Ship the export for new …"
ESCALATION_HEADING: Final = "api · T-1 · Pick the layout · S41"
INFORMATION_HEADING: Final = "api · T-1 · Pick the layout · For information · S41"
NOTES_BLOCK: Final = f"What I tried: {NOTE}"


def _asked(options: list[dict[str, object]], *, resolutions: list[dict[str, object]]) -> Message:
    return escalation_message(
        NUMBER,
        ESCALATION,
        _view(options=options, resolutions=resolutions),
        title=TITLE,
        in_topic=False,
        note=NOTE,
        suggestion="",
        label_max_chars=BUTTON_LABEL_MAX_CHARS,
    )


@pytest.mark.parametrize(
    ("message", "heading", "labels", "blocks"),
    [
        pytest.param(
            _asked(
                [_option("Keep", recommended=True), _option("Change")],
                resolutions=[_resolution("cancel")],
            ),
            ESCALATION_HEADING,
            ["★ 1. Keep", "2. Change", "Cancel task"],
            ["Which?", "★ 1. Keep\n2. Change\nCancel task", NOTES_BLOCK],
            id="recommended-option",
        ),
        pytest.param(
            _asked([_option("Keep"), _option("Change")], resolutions=[_resolution("cancel")]),
            ESCALATION_HEADING,
            ["1. Keep", "2. Change", "Cancel task"],
            ["Which?", "1. Keep\n2. Change\nCancel task", NOTES_BLOCK],
            id="no-recommended-option",
        ),
        pytest.param(
            _asked([_option(LONG_LABEL, recommended=True)], resolutions=[]),
            ESCALATION_HEADING,
            [LONG_LABEL_CUT],
            ["Which?", f"★ 1. {LONG_LABEL}", NOTES_BLOCK],
            id="long-label",
        ),
        pytest.param(
            information_message(
                NUMBER, INFORMATION, title=TITLE, in_topic=False, note=NOTE, suggestion=""
            ),
            INFORMATION_HEADING,
            ["Acknowledge"],
            ["Task finished", NOTES_BLOCK],
            id="information",
        ),
    ],
)
def test_message_has_a_heading_then_blank_line_separated_blocks_and_short_labels(
    message: Message, heading: str, labels: list[str], blocks: list[str]
) -> None:
    assert message.heading == heading
    assert message.text.split("\n\n") == blocks
    assert [button.label for button in message.buttons] == labels


def test_message_over_the_limit_is_cut_with_a_details_button() -> None:
    view = _view(question="Q" * (2 * MESSAGE_TEXT_LIMIT), resolutions=[_resolution("retry")])
    message = escalation_message(
        NUMBER,
        ESCALATION,
        view,
        title=TITLE,
        in_topic=False,
        note=NOTE,
        suggestion="",
        label_max_chars=BUTTON_LABEL_MAX_CHARS,
    )
    assert len(message.shown()) == MESSAGE_TEXT_LIMIT
    assert message.text.endswith(CUT_MARK)
    assert message.buttons[-1] == Button("Details", "d:41")


def test_callback_data_fits_the_platform_limit() -> None:
    options = [_option(f"option {number}") for number in range(30)]
    resolutions = [_resolution(kind) for kind in ("answer", "retry", "accept", "cancel")]
    view = _view(options=options, resolutions=resolutions)
    message = escalation_message(
        123456,
        ESCALATION,
        view,
        title=TITLE,
        in_topic=False,
        note=NOTE,
        suggestion="",
        label_max_chars=BUTTON_LABEL_MAX_CHARS,
    )
    assert message.buttons
    assert all(len(button.data.encode()) <= CALLBACK_DATA_LIMIT for button in message.buttons)


def test_forwarder_queues_one_message_per_destination_keyed_by_the_signal() -> None:
    outbox = _Outbox()
    forwarder = _forwarder(_Client(view=_fixture_view()), outbox, "1001", "1002")
    asyncio.run(forwarder.forward(NUMBER, ESCALATION, NOTE, SUGGESTION))
    assert [
        (purpose, destinations[0].chat, key) for purpose, destinations, _, key in outbox.queued
    ] == [(Purpose.QUESTION, "1001", "forward:41"), (Purpose.QUESTION, "1002", "forward:41")]
    message = outbox.queued[0][2]
    assert "S41" in message.heading
    assert "Which behavior should remain?" in message.text
    assert SUGGESTION in message.text


@pytest.mark.parametrize(
    ("private", "signal", "title", "title_fails", "header"),
    [
        pytest.param(
            False,
            ESCALATION,
            TITLE,
            False,
            "T-1 · Pick the layout · S41",
            id="component-topic",
        ),
        pytest.param(
            False,
            INFORMATION,
            TITLE,
            False,
            "api · T-1 · Pick the layout · For information · S41",
            id="general-topic-information",
        ),
        pytest.param(
            True,
            ESCALATION,
            TITLE,
            False,
            "api · T-1 · Pick the layout · S41",
            id="private-chat",
        ),
        pytest.param(False, ESCALATION, TITLE, True, "T-1 · S41", id="title-unavailable"),
        pytest.param(
            True,
            ESCALATION,
            TITLE,
            True,
            "api · T-1 · S41",
            id="private-chat-title-unavailable",
        ),
        pytest.param(
            False,
            ESCALATION,
            LONG_TITLE,
            False,
            f"T-1 · {CUT_TITLE} · S41",
            id="title-over-the-limit",
        ),
    ],
)
def test_decision_header_names_the_project_only_outside_its_topic(
    topics: TopicStore,
    *,
    private: bool,
    signal: Signal,
    title: str,
    title_fails: bool,
    header: str,
) -> None:
    group_id = None if private else -1009
    destinations = TelegramDestinations(topics, owners=(1001,), group_id=group_id, reconciler=None)
    client = _Client(
        _fixture_view(),
        title=title,
        task_error=ShikiruLinkDownError("link down") if title_fails else None,
    )
    outbox = _Outbox()
    forwarder = ProductForwarder(
        MESSAGE_TITLE_MAX_CHARS,
        {"api": cast("ShikiruClient", client)},
        outbox,
        _Clock(),
        destinations,
        button_label_max_chars=BUTTON_LABEL_MAX_CHARS,
    )
    asyncio.run(forwarder.forward(NUMBER, signal, NOTE, ""))
    [(_, _, message, _)] = outbox.queued
    assert message.heading == header


def test_escalation_that_shikiru_no_longer_returns_is_shown_as_information() -> None:
    outbox = _Outbox()
    asyncio.run(
        _forwarder(_Client(view=None), outbox, "1001").forward(NUMBER, ESCALATION, NOTE, "")
    )
    purpose, _, message, _ = outbox.queued[0]
    assert purpose is Purpose.NOTICE
    assert message.buttons == (Button("Acknowledge", "k:41"),)


def test_signal_without_escalation_is_shown_as_information_without_asking_shikiru() -> None:
    client = _Client(error=ShikiruLinkDownError("link down"))
    outbox = _Outbox()
    engine_down = Signal("api", Kind.ENGINE_DOWN, "7", "engine_stopped", "The engine stopped.")
    asyncio.run(_forwarder(client, outbox, "1001").forward(NUMBER, engine_down, NOTE, ""))
    assert client.asked == []
    assert outbox.queued[0][2].buttons == (Button("Acknowledge", "k:41"),)


def test_link_down_while_reading_the_escalation_propagates_for_a_retry() -> None:
    client = _Client(error=ShikiruLinkDownError("link down"))
    outbox = _Outbox()
    with pytest.raises(ShikiruLinkDownError):
        asyncio.run(_forwarder(client, outbox, "1001").forward(NUMBER, ESCALATION, NOTE, ""))
    assert outbox.queued == []


def test_product_handler_forwards_with_nothing_tried() -> None:
    outcome = asyncio.run(ProductHandler().handle(ESCALATION))
    assert outcome == Forward(NOTHING_TRIED)
