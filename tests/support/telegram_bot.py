"""A fake Bot API for tests: it records each call and answers with a documented reply."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

SEND_OK: Final = Path(__file__).resolve().parents[1] / "fixtures" / "telegram" / "send_ok.json"


class FakeBotApi:
    """Answers every method with the sendMessage fixture; `called` is set by the first call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.called = asyncio.Event()

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        self.calls.append((method, dict(payload)))
        self.called.set()
        return json.loads(SEND_OK.read_text(encoding="utf-8"))
