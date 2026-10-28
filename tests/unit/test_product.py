from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Final, cast

import pytest

from shikiri_yaku.domain.outcome import Forward
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.extensions.product.forwarder import ProductForwarder
from shikiri_yaku.extensions.product.handler import NOTHING_TRIED, ProductHandler
from shikiri_yaku.extensions.product.message import (
    CUT_MARK,
    RECOMMENDED,
    escalation_message,
)
from shikiri_yaku.ports import (
    CALLBACK_DATA_LIMIT,
    MESSAGE_TEXT_LIMIT,
    Button,
    EscalationView,
    Message,
    ShikiruClient,
    ShikiruLinkDownError,
)

_FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "shikiru"
NUMBER: Final = 41
PRODUCT: Final = "Shop"
NOTE: Final = "nothing, this is a product question"
SUGGESTION: Final = "add it to pyproject.toml"
ESCALATION: Final = Signal(
    "api", Kind.ESCALATION, "E-1", "decision_required", "A product decision", "T-1", "E-1"
)


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


def _option(label: str, *, needs_input: bool = False) -> dict[str, object]:
    return {"label": label, "consequences": "", "recommended": False, "needs_input": needs_input}


def _resolution(kind: str, *, recommended: bool = False) -> dict[str, object]:
    return {"kind": kind, "consequences": "", "recommended": recommended}


class _Client:
    def __init__(self, view: EscalationView | None = None, error: Exception | None = None) -> None:
        self.view = view
        self.error = error
        self.asked: list[str] = []

    async def get_escalation(self, escalation_id: str) -> EscalationView | None:
        self.asked.append(escalation_id)
        if self.error is not None:
            raise self.error
        return self.view


class _Channel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Message]] = []

    async def send(self, destination: str, message: Message) -> str:
        self.sent.append((destination, message))
        return str(len(self.sent))

    async def edit(self, message_ref: str, message: Message) -> None:
        del message_ref, message
        raise NotImplementedError


def test_fixture_view_message_has_question_options_and_one_recommended_mark() -> None:
    message = escalation_message(
        NUMBER, ESCALATION, _fixture_view(), product=PRODUCT, note=NOTE, suggestion=""
    )
    assert "Which behavior should remain?" in message.text
    assert "Keep existing behavior" in message.text
    assert "Change behavior" in message.text
    assert "Preserve the current call pattern." in message.text
    assert message.text.count(RECOMMENDED) == 1
    assert "## " not in message.text
    assert "`" not in message.text


def test_fixture_view_offers_options_then_resolutions_with_numbered_callback_data() -> None:
    message = escalation_message(
        NUMBER, ESCALATION, _fixture_view(), product=PRODUCT, note=NOTE, suggestion=""
    )
    assert message.buttons == (
        Button("1", "a:41:1"),
        Button("2", "a:41:2"),
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
        NUMBER, ESCALATION, view, product=PRODUCT, note=NOTE, suggestion=""
    )
    assert list(message.buttons) == expected


@pytest.mark.parametrize("needs_input", [False, True])
def test_option_that_needs_input_uses_the_same_callback_as_the_others(*, needs_input: bool) -> None:
    view = _view(options=[_option("Keep", needs_input=needs_input)])
    message = escalation_message(
        NUMBER, ESCALATION, view, product=PRODUCT, note=NOTE, suggestion=""
    )
    assert message.buttons == (Button("1", "a:41:1"),)


def test_resolution_kind_the_operator_does_not_know_is_not_offered_and_keeps_its_number() -> None:
    view = _view(resolutions=[_resolution("skip"), _resolution("retry")])
    message = escalation_message(
        NUMBER, ESCALATION, view, product=PRODUCT, note=NOTE, suggestion=""
    )
    assert message.buttons == (Button("Retry", "a:41:2"),)


def test_recommended_resolution_is_marked_only_when_there_are_no_agent_options() -> None:
    view = _view(resolutions=[_resolution("retry", recommended=True)])
    message = escalation_message(
        NUMBER, ESCALATION, view, product=PRODUCT, note=NOTE, suggestion=""
    )
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
    message = escalation_message(NUMBER, signal, view, product=PRODUCT, note=NOTE, suggestion="")
    assert ("Log in to the agent again" in message.text) is hinted


def test_message_over_the_limit_is_cut_with_a_details_button() -> None:
    view = _view(question="Q" * (2 * MESSAGE_TEXT_LIMIT), resolutions=[_resolution("retry")])
    message = escalation_message(
        NUMBER, ESCALATION, view, product=PRODUCT, note=NOTE, suggestion=""
    )
    assert len(message.text) == MESSAGE_TEXT_LIMIT
    assert message.text.endswith(CUT_MARK)
    assert message.buttons[-1] == Button("Details", "d:41")


def test_callback_data_fits_the_platform_limit() -> None:
    options = [_option(f"option {number}") for number in range(30)]
    resolutions = [_resolution(kind) for kind in ("answer", "retry", "accept", "cancel")]
    view = _view(options=options, resolutions=resolutions)
    message = escalation_message(
        123456, ESCALATION, view, product=PRODUCT, note=NOTE, suggestion=""
    )
    assert message.buttons
    assert all(len(button.data.encode()) <= CALLBACK_DATA_LIMIT for button in message.buttons)


def test_forwarder_sends_the_same_message_to_every_destination() -> None:
    client = _Client(view=_fixture_view())
    channel = _Channel()
    forwarder = ProductForwarder(
        PRODUCT, {"api": cast("ShikiruClient", client)}, channel, ["1001", "1002"]
    )
    asyncio.run(forwarder.forward(NUMBER, ESCALATION, NOTE, SUGGESTION))
    assert [destination for destination, _ in channel.sent] == ["1001", "1002"]
    first, second = (message for _, message in channel.sent)
    assert first == second
    assert "S41" in first.text
    assert "Which behavior should remain?" in first.text
    assert SUGGESTION in first.text


def test_escalation_that_shikiru_no_longer_returns_is_shown_as_information() -> None:
    client = _Client(view=None)
    channel = _Channel()
    forwarder = ProductForwarder(PRODUCT, {"api": cast("ShikiruClient", client)}, channel, ["1001"])
    asyncio.run(forwarder.forward(NUMBER, ESCALATION, NOTE, ""))
    assert channel.sent[0][1].buttons == (Button("Acknowledge", "k:41"),)


def test_signal_without_escalation_is_shown_as_information_without_asking_shikiru() -> None:
    client = _Client(error=ShikiruLinkDownError("link down"))
    channel = _Channel()
    forwarder = ProductForwarder(PRODUCT, {"api": cast("ShikiruClient", client)}, channel, ["1001"])
    engine_down = Signal("api", Kind.ENGINE_DOWN, "7", "engine_stopped", "The engine stopped.")
    asyncio.run(forwarder.forward(NUMBER, engine_down, NOTE, ""))
    assert client.asked == []
    assert channel.sent[0][1].buttons == (Button("Acknowledge", "k:41"),)


def test_link_down_while_reading_the_escalation_propagates_for_a_retry() -> None:
    client = _Client(error=ShikiruLinkDownError("link down"))
    channel = _Channel()
    forwarder = ProductForwarder(PRODUCT, {"api": cast("ShikiruClient", client)}, channel, ["1001"])
    with pytest.raises(ShikiruLinkDownError):
        asyncio.run(forwarder.forward(NUMBER, ESCALATION, NOTE, ""))
    assert channel.sent == []


def test_product_handler_forwards_with_nothing_tried() -> None:
    outcome = asyncio.run(ProductHandler().handle(ESCALATION))
    assert outcome == Forward(NOTHING_TRIED)
