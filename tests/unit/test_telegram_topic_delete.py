"""Deleting an Expert topic: which refusals end the conversation and which are retried."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Final

import pytest

from shikiri_yaku.adapters.telegram import PLATFORM_REFUSED, TelegramForumTopics
from shikiri_yaku.ports import TOPIC_GONE_REASON, ChannelError, ChannelRefusedError

if TYPE_CHECKING:
    from collections.abc import Mapping

GROUP_ID: Final = -100123
THREAD: Final = 22


class _Api:
    def __init__(self, body: object) -> None:
        self.body = body
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        self.calls.append((method, dict(payload)))
        return self.body


def _delete(body: object) -> _Api:
    api = _Api(body)
    asyncio.run(TelegramForumTopics(api, GROUP_ID).delete(THREAD))
    return api


def test_a_deleted_topic_is_named_by_the_group_and_the_thread() -> None:
    api = _delete({"ok": True, "result": True})

    assert api.calls == [
        ("deleteForumTopic", {"chat_id": GROUP_ID, "message_thread_id": THREAD}),
    ]


@pytest.mark.parametrize(
    ("body", "error", "reason"),
    [
        pytest.param(
            {"ok": False, "error_code": 400, "description": "Bad Request: thread not found"},
            ChannelRefusedError,
            TOPIC_GONE_REASON,
            id="thread-not-found",
        ),
        pytest.param(
            {"ok": False, "error_code": 400, "description": "Bad Request: TOPIC_ID_INVALID"},
            ChannelRefusedError,
            TOPIC_GONE_REASON,
            id="topic-id-invalid",
        ),
        pytest.param(
            {"ok": False, "error_code": 400, "description": "Bad Request: not enough rights"},
            ChannelRefusedError,
            PLATFORM_REFUSED,
            id="other-bad-request",
        ),
        pytest.param(
            {"ok": False, "error_code": 403, "description": "Forbidden"},
            ChannelRefusedError,
            PLATFORM_REFUSED,
            id="forbidden",
        ),
        pytest.param(
            {"ok": False, "error_code": 500, "description": "Internal"},
            ChannelError,
            None,
            id="server-error-is-retried",
        ),
    ],
)
def test_a_refused_deletion_says_whether_the_topic_is_gone(
    body: dict[str, Any], error: type[Exception], reason: str | None
) -> None:
    with pytest.raises(error) as raised:
        _delete(body)

    if reason is not None:
        assert str(raised.value) == reason
