"""What the PO sends: getUpdates read as the Bot API documents it.

No socket is opened and no token is read; the Bot API is a fake that returns a fixture.
"""

from __future__ import annotations

import asyncio
import copy
import json
import traceback
from typing import TYPE_CHECKING, Any, Final

import pytest

from shikiri_yaku.adapters.telegram_updates import TelegramUpdates
from shikiri_yaku.domain.inbound import Batch, Inbound, InboundKind
from shikiri_yaku.ports import ChannelError
from tests.support.secrets import SECRETS
from tests.support.telegram_bot import FIXTURES

if TYPE_CHECKING:
    from collections.abc import Mapping

OWNER: Final = 1001
GROUP: Final = -1005
OTHER_GROUP: Final = -1006
WAIT_S: Final = 20
UPDATES: Final = json.loads((FIXTURES / "get_updates_ok.json").read_text(encoding="utf-8"))
TEXT, REPLY, TAP = 700, 701, 702
TOPIC_TEXT: Final = 710


class _Api:
    def __init__(self, body: object) -> None:
        self.body = body
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        self.calls.append((method, dict(payload)))
        return self.body


def _read(body: object) -> Batch:
    return asyncio.run(TelegramUpdates(_Api(body), group_id=GROUP).fetch(0, WAIT_S))


def test_each_documented_update_becomes_the_item_it_stands_for() -> None:
    api = _Api(UPDATES)
    batch = asyncio.run(TelegramUpdates(api, group_id=GROUP).fetch(705, WAIT_S))

    assert api.calls == [
        (
            "getUpdates",
            {"offset": 705, "timeout": WAIT_S, "allowed_updates": ["message", "callback_query"]},
        )
    ]
    by_id = {item.update_id: item for item in batch.items}
    assert batch.last_update_id == TAP
    assert set(by_id) == {TEXT, REPLY, TAP}
    assert by_id[TEXT] == Inbound(
        TEXT, InboundKind.TEXT, OWNER, OWNER, "1001/90", text="Use the second way"
    )
    assert by_id[REPLY].kind == InboundKind.REPLY
    assert (by_id[REPLY].message_ref, by_id[REPLY].reply_to_ref) == ("1001/91", "1001/42")
    assert by_id[TAP] == Inbound(TAP, InboundKind.TAP, OWNER, OWNER, "1001/42", data="a:1:1")


def test_a_message_in_a_forum_topic_keeps_its_group_chat_and_its_topic() -> None:
    body = {
        "ok": True,
        "result": [
            {
                "update_id": TOPIC_TEXT,
                "message": {
                    "message_id": 33,
                    "from": {"id": OWNER},
                    "chat": {"id": GROUP},
                    "message_thread_id": 77,
                    "text": "/goal Add the export",
                },
            }
        ],
    }

    [item] = _read(body).items

    assert item == Inbound(
        TOPIC_TEXT,
        InboundKind.TEXT,
        OWNER,
        GROUP,
        f"{GROUP}/33",
        text="/goal Add the export",
        thread=77,
    )


@pytest.mark.parametrize(("chat", "kept"), [(OWNER, True), (GROUP, True), (OTHER_GROUP, False)])
def test_an_item_comes_only_from_a_private_chat_or_the_configured_group(
    chat: int, *, kept: bool
) -> None:
    body = {
        "ok": True,
        "result": [
            {
                "update_id": 720,
                "message": {
                    "message_id": 34,
                    "from": {"id": OWNER},
                    "chat": {"id": chat},
                    "text": "hi",
                },
            }
        ],
    }

    batch = _read(body)

    assert (batch.last_update_id, [item.update_id for item in batch.items]) == (
        720,
        [720] if kept else [],
    )


def test_a_kind_the_operator_does_not_read_moves_the_batch_but_gives_no_item() -> None:
    body: dict[str, Any] = {"ok": True, "result": [{"update_id": 704, "edited_message": {}}]}

    batch = _read(body)

    assert (batch.last_update_id, batch.items) == (704, ())


@pytest.mark.parametrize(("index", "field"), [(0, "from"), (1, "chat"), (2, "data")])
def test_a_supported_update_missing_a_required_field_fails_the_whole_batch(
    index: int, field: str
) -> None:
    body = copy.deepcopy(UPDATES)
    update = body["result"][index]
    del update["callback_query" if index == 2 else "message"][field]

    with pytest.raises(ChannelError):
        _read(body)


_UNREADABLE: Final[list[object]] = [
    {"ok": False, "error_code": 409, "description": "Conflict"},
    {"ok": True, "result": [{"message": {}}]},
    {"ok": True},
    [],
]


_LEAKING: Final[list[object]] = [
    {"ok": True, "result": [{"text": SECRETS["sk_key"]}]},
    {"ok": True, "result": SECRETS["sk_key"]},
    {"ok": True, "result": [{"update_id": 1, "message": {"text": SECRETS["sk_key"]}}]},
]


@pytest.mark.parametrize("body", _LEAKING)
def test_a_failed_poll_does_not_carry_the_payload_in_its_traceback(body: object) -> None:
    with pytest.raises(ChannelError) as raised:
        _read(body)

    rendered = "".join(traceback.format_exception(raised.value))
    assert SECRETS["sk_key"] not in rendered


@pytest.mark.parametrize("body", _UNREADABLE)
def test_a_reply_that_is_not_a_list_of_updates_fails_the_poll(body: object) -> None:
    with pytest.raises(ChannelError):
        _read(body)
