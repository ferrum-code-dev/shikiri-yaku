"""A source-created escalation ends as a sent message with buttons, through the composed jobs.

The Shikiru link and the Bot API are fakes, so no process and no network take part (design §7, §12).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from mcp.types import CallToolResult

from shikiri_yaku.adapters.clock import SystemClock
from shikiri_yaku.bootstrap import compose
from shikiri_yaku.instance import JOURNAL_FILE_NAME
from shikiri_yaku.journal import Database
from shikiri_yaku.settings import load_settings
from tests.support.telegram_bot import FakeBotApi

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from shikiri_yaku.adapters.shikiru_mcp import ToolSession
    from shikiri_yaku.cli import Job

_PRODUCT: Final = "shop"
_OWNER: Final = 123456789
_FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "shikiru"
_DELIVERY_TIMEOUT_S: Final = 20


class _ShikiruSession:
    """Answers the tools of one healthy project whose only event is an open escalation."""

    async def call_tool(self, name: str, arguments: dict[str, Any], *_ignored: object) -> object:
        if name == "wait_for_events":
            await SystemClock().sleep(arguments["timeout_s"])
            payload: dict[str, Any] = {"result": []}
        elif name == "status":
            payload = {"engine_up": True, "quotas": {}}
        else:
            payload = json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        return CallToolResult(content=[], structuredContent=payload, isError=False)


@asynccontextmanager
async def _shikiru_link() -> AsyncGenerator[ToolSession]:
    yield _ShikiruSession()  # pyright: ignore[reportReturnType]


def _instance(home: Path) -> Path:
    component = home / "api"
    component.mkdir()
    directory = home / _PRODUCT
    directory.mkdir()
    (directory / "config.toml").write_text(
        f'product = "{_PRODUCT}"\n\n[[components]]\nname = "api"\npath = "{component}"\n\n'
        f"[channels.telegram]\nowners = [{_OWNER}]\n\n"
        "[operator]\ndispatch_interval_s = 1\noutbox_interval_s = 1\n",
        encoding="utf-8",
    )
    return directory


async def _deliver(jobs: Sequence[Job], bot: FakeBotApi) -> None:
    """Run the composed jobs until the Bot API is first asked, then stop every job."""
    running = [asyncio.create_task(job()) for job in jobs]
    try:
        async with asyncio.timeout(_DELIVERY_TIMEOUT_S):
            await bot.called.wait()
    finally:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


def test_a_source_escalation_ends_as_a_sent_message_with_buttons(tmp_path: Path) -> None:
    directory = _instance(tmp_path)
    bot = FakeBotApi()
    composition = compose(
        load_settings(directory),
        directory,
        link=lambda _executable, _project: _shikiru_link,
        bot=lambda _settings: bot,
    )
    try:
        asyncio.run(_deliver(composition.jobs, bot))
    finally:
        composition.close()

    [(method, payload)] = bot.calls
    assert method == "sendMessage"
    assert payload["chat_id"] == str(_OWNER)
    assert payload["reply_markup"]["inline_keyboard"]
    database = Database.open(directory / JOURNAL_FILE_NAME)
    try:
        rows = database.connection.execute("SELECT platform_message_id FROM messages")
        references = [row["platform_message_id"] for row in rows]
    finally:
        database.close()
    assert references == ["1001/42"]
