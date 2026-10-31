"""A fake Bot API for tests: it records each call and answers with a documented reply."""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "telegram"
SEND_OK: Final = FIXTURES / "send_ok.json"
GET_ME_OK: Final = FIXTURES / "get_me_ok.json"
GET_WEBHOOK_INFO_OK: Final = FIXTURES / "get_webhook_info.json"
GET_ME: Final = "getMe"
GET_UPDATES: Final = "getUpdates"
GET_WEBHOOK_INFO: Final = "getWebhookInfo"
# keep: the message id the sendMessage fixture carries, so the first message sent is 1001/42
FIRST_MESSAGE_ID: Final = 42


class FakeBotApi:
    """Answers getMe and getWebhookInfo with their fixtures and every other method with sendMessage.

    Each reply carries the next message id, as Telegram gives each message its own.
    getUpdates gives `updates` once a message was sent and nothing before or after, or at the
    first poll when `after_send` is false.
    `called` is set by the first call that is not a getUpdates.
    """

    def __init__(
        self, updates: list[dict[str, Any]] | None = None, *, after_send: bool = True
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.called = asyncio.Event()
        self._updates = updates or []
        self._after_send = after_send
        self._message_ids = itertools.count(FIRST_MESSAGE_ID)

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        self.calls.append((method, dict(payload)))
        if method == GET_UPDATES:
            sent = not self._after_send or any(name != GET_UPDATES for name, _ in self.calls)
            result, self._updates = (self._updates, []) if sent else ([], self._updates)
            return {"ok": True, "result": result}
        self.called.set()
        if method == GET_ME:
            return json.loads(GET_ME_OK.read_text(encoding="utf-8"))
        if method == GET_WEBHOOK_INFO:
            return json.loads(GET_WEBHOOK_INFO_OK.read_text(encoding="utf-8"))
        reply: dict[str, Any] = json.loads(SEND_OK.read_text(encoding="utf-8"))
        reply["result"]["message_id"] = next(self._message_ids)
        return reply
