"""A source-created escalation ends as a sent message with buttons, through the composed jobs.

A goal whose submit a crash interrupted is asked about after the restart, and never submitted again.
The Shikiru link and the Bot API are fakes, so no process and no network take part (design §7, §12).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest
from mcp.types import CallToolResult

from shikiri_yaku.adapters.clock import SystemClock
from shikiri_yaku.bootstrap import compose
from shikiri_yaku.instance import JOURNAL_FILE_NAME
from shikiri_yaku.journal import Database, JournalInbound, OpStore
from shikiri_yaku.journal.submits import JournalSubmits
from shikiri_yaku.ports import SubmitStatus
from shikiri_yaku.settings import load_settings
from tests.support.telegram_bot import FakeBotApi

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager

    from shikiri_yaku.adapters.shikiru_mcp import ToolSession
    from shikiri_yaku.cli import Job

_PRODUCT: Final = "shop"
_OWNER: Final = 1001
_FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "shikiru"
_DELIVERY_TIMEOUT_S: Final = 20
_ASKED: Final = "I may have submitted this already"
TELEGRAM_FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "telegram"


class _ShikiruSession:
    """Answers the tools of one healthy project whose only event is an open escalation.

    A submit is recorded and answered, unless `hangs_submit` keeps it from being answered, as a
    process that dies inside the call leaves it.
    """

    def __init__(self, resolved: asyncio.Event, *, hangs_submit: bool = False) -> None:
        self.resolved = resolved
        self.resolutions: list[dict[str, Any]] = []
        self.submits: list[str] = []
        self.submitted = asyncio.Event()
        self.hangs_submit = hangs_submit

    async def call_tool(self, name: str, arguments: dict[str, Any], *_ignored: object) -> object:
        if name == "wait_for_events":
            await SystemClock().sleep(arguments["timeout_s"])
            payload: dict[str, Any] = {"result": []}
        elif name == "status":
            payload = {"engine_up": True, "quotas": {}}
        elif name == "resolve_escalation":
            self.resolutions.append(arguments)
            self.resolved.set()
            payload = {"id": 12, "status": "queued", "result": None, "message": None}
        elif name == "submit_task":
            self.submits.append(arguments["request"])
            self.submitted.set()
            if self.hangs_submit:
                await asyncio.Event().wait()
            payload = {"id": 7, "status": "done", "result": {"task_id": "T-9"}, "message": None}
        else:
            payload = json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        return CallToolResult(content=[], structuredContent=payload, isError=False)


def _shikiru_link(
    session: _ShikiruSession,
) -> Callable[[], AbstractAsyncContextManager[ToolSession]]:
    @asynccontextmanager
    async def link() -> AsyncGenerator[ToolSession]:
        yield session  # pyright: ignore[reportReturnType]

    return link


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


async def _deliver(jobs: Sequence[Job], done: asyncio.Event) -> None:
    """Run the composed jobs until `done` is set, then stop every job."""
    running = [asyncio.create_task(job()) for job in jobs]
    try:
        async with asyncio.timeout(_DELIVERY_TIMEOUT_S):
            await done.wait()
    finally:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


class _AskingBot(FakeBotApi):
    """A fake Bot API that also sets `asked` once the question about an unknown goal is sent."""

    def __init__(self) -> None:
        super().__init__()
        self.asked = asyncio.Event()

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        if _ASKED in str(payload.get("text", "")):
            self.asked.set()
        return await super().call(method, payload)


def test_a_source_escalation_ends_as_a_sent_message_with_buttons(tmp_path: Path) -> None:
    directory = _instance(tmp_path)
    bot = FakeBotApi()
    session = _ShikiruSession(asyncio.Event())
    composition = compose(
        load_settings(directory),
        directory,
        link=lambda _executable, _project: _shikiru_link(session),
        bot=lambda _settings: bot,
    )
    try:
        asyncio.run(_deliver(composition.jobs, bot.called))
    finally:
        composition.close()

    method, payload = next(call for call in bot.calls if call[0] != "getUpdates")
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


def test_a_tap_runs_through_replies_to_a_journaled_resolve(tmp_path: Path) -> None:
    directory = _instance(tmp_path)
    tap = json.loads((TELEGRAM_FIXTURES / "get_updates_ok.json").read_text(encoding="utf-8"))
    [update] = tap["result"][2:]
    update["callback_query"]["from"]["id"] = _OWNER
    bot = FakeBotApi(updates=[update])
    session = _ShikiruSession(asyncio.Event())
    composition = compose(
        load_settings(directory),
        directory,
        link=lambda _executable, _project: _shikiru_link(session),
        bot=lambda _settings: bot,
    )
    try:
        asyncio.run(_deliver(composition.jobs, session.resolved))
    finally:
        composition.close()

    assert session.resolutions == [
        {
            "escalation_id": "E-1",
            "resolution": {
                "kind": "answer",
                "text": "Keep existing behavior (Preserve the current call pattern.)",
            },
        }
    ]
    database = Database.open(directory / JOURNAL_FILE_NAME)
    try:
        [op] = database.connection.execute(
            "SELECT state, command_id FROM ops WHERE kind = 'resolve'"
        )
        offset = database.connection.execute("SELECT next_offset FROM polling").fetchone()
    finally:
        database.close()
    assert (op["state"], op["command_id"], offset["next_offset"]) == ("issued", "12", 703)


def _goal_update() -> dict[str, Any]:
    """Return the owner's `/goal` as a text update, from the fixture's text message."""
    updates = json.loads((TELEGRAM_FIXTURES / "get_updates_ok.json").read_text(encoding="utf-8"))
    update = updates["result"][0]
    update["message"]["text"] = "/goal api Add the export"
    update["message"]["from"]["id"] = _OWNER
    update["message"]["chat"]["id"] = _OWNER
    return update


@pytest.mark.parametrize("handled", [False, True], ids=["inbound-pending", "inbound-handled"])
def test_a_restart_asks_about_a_goal_interrupted_mid_submit_and_never_submits_it_again(
    tmp_path: Path, *, handled: bool
) -> None:
    directory = _instance(tmp_path)
    crashed = _ShikiruSession(asyncio.Event(), hangs_submit=True)
    composition = compose(
        load_settings(directory),
        directory,
        link=lambda _executable, _project: _shikiru_link(crashed),
        bot=lambda _settings: FakeBotApi([_goal_update()], after_send=False),
    )
    try:
        asyncio.run(_deliver(composition.jobs, crashed.submitted))
    finally:
        composition.close()
    database = Database.open(directory / JOURNAL_FILE_NAME)
    try:
        [interrupted] = JournalSubmits(database, OpStore(database)).unfinished()
        if handled:
            JournalInbound(database).mark_handled(_goal_update()["update_id"])
    finally:
        database.close()
    assert (crashed.submits, interrupted.status) == (["Add the export"], SubmitStatus.ISSUED)

    restarted = _ShikiruSession(asyncio.Event())
    bot = _AskingBot()
    composition = compose(
        load_settings(directory),
        directory,
        link=lambda _executable, _project: _shikiru_link(restarted),
        bot=lambda _settings: bot,
    )
    try:
        composition.recover()
        asyncio.run(_deliver(composition.jobs, bot.asked))
    finally:
        composition.close()

    assert restarted.submits == []
    [question] = [payload for _method, payload in bot.calls if _ASKED in str(payload.get("text"))]
    [[button]] = question["reply_markup"]["inline_keyboard"]
    assert button["callback_data"] == f"s:{interrupted.id}"
    database = Database.open(directory / JOURNAL_FILE_NAME)
    try:
        row = database.connection.execute(
            "SELECT COUNT(*) AS asked FROM messages WHERE notice_key = ?",
            (f"goal:{interrupted.id}",),
        ).fetchone()
    finally:
        database.close()
    assert row["asked"] == 1
