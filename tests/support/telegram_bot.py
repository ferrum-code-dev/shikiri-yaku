"""A fake Bot API for tests: it records each call and answers with a documented reply."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "telegram"
SEND_OK: Final = FIXTURES / "send_ok.json"
GET_ME_OK: Final = FIXTURES / "get_me_ok.json"
GET_ME: Final = "getMe"


class FakeBotApi:
    """Answers getMe with its fixture and every other method with the sendMessage fixture.

    `called` is set by the first call.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.called = asyncio.Event()

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        self.calls.append((method, dict(payload)))
        self.called.set()
        reply = GET_ME_OK if method == GET_ME else SEND_OK
        return json.loads(reply.read_text(encoding="utf-8"))
