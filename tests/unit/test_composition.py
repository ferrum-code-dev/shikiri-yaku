"""A source-created escalation ends as a sent message with buttons, through the composed jobs.

A goal whose submit a crash interrupted is asked about after the restart, and never submitted again.
The Shikiru link and the Bot API are fakes, so no process and no network take part (design §7, §12).
"""

from __future__ import annotations

import asyncio
import itertools
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest
from mcp.types import CallToolResult

from shikiri_yaku.adapters.clock import SystemClock
from shikiri_yaku.app.event_source import MERGED_LINE
from shikiri_yaku.app.health_source import RECOVERED_LINE
from shikiri_yaku.app.loop_guard import LoopGuard
from shikiri_yaku.bootstrap import REMOVED_NOTE, compose
from shikiri_yaku.domain.outbound import ENGINE_UP_LINE
from shikiri_yaku.domain.signal import Kind, Signal
from shikiri_yaku.extensions.infrastructure.rules import AGENT_ERROR, QUOTA_EXHAUSTED
from shikiri_yaku.instance import JOURNAL_FILE_NAME
from shikiri_yaku.journal import (
    Database,
    JournalActions,
    JournalInbound,
    JournalStarts,
    NewSignal,
    OpStore,
    SignalState,
    SignalStore,
)
from shikiri_yaku.journal.submits import JournalSubmits
from shikiri_yaku.ports import (
    AttemptState,
    CommandResult,
    EventKinds,
    ResolutionKind,
    SubmitStatus,
)
from shikiri_yaku.settings import load_settings
from tests.support.clock import NapClock
from tests.support.telegram_bot import FIRST_MESSAGE_ID, FakeBotApi

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager
    from datetime import datetime

    from shikiri_yaku.adapters.shikiru_mcp import ToolSession
    from shikiri_yaku.cli import Job

_PRODUCT: Final = "shop"
_OWNER: Final = 1001
_GROUP: Final = -1009
_TOPIC: Final = 73
_PROMPT_ID: Final = FIRST_MESSAGE_ID + 1
_HELP_LINE: Final = "/help · this list"
_FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "shikiru"
_DELIVERY_TIMEOUT_S: Final = 20
_ASKED: Final = "I may have submitted this already"
_RESTARTED: Final = "The engine of api was restarted."
_FAILED_HANDLER: Final = "handler failed"
_ENGINE_DOWN: Final = Signal("api", Kind.ENGINE_DOWN, "status", "", "engine is down")
_ENGINE_DOWN_NEW: Final = NewSignal(_ENGINE_DOWN.id, "api", "engine_down", "", "engine is down")
_REMOVED_DOWN: Final = Signal("old", Kind.ENGINE_DOWN, "status", "", "engine is down")
_REMOVED_DOWN_NEW: Final = NewSignal(_REMOVED_DOWN.id, "old", "engine_down", "", "engine is down")
TELEGRAM_FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "telegram"
_FORUM_TOPIC: Final = TELEGRAM_FIXTURES / "forum_topic_created.json"
_CONFLICT: Final[dict[str, Any]] = json.loads(
    (TELEGRAM_FIXTURES / "get_updates_conflict.json").read_text(encoding="utf-8")
)
_E1_ID: Final = "api:escalation:E-1"
_INSTALL_COMMAND: Final = "brew install ffmpeg"
_FOUND_PATH: Final = "/opt/homebrew/bin/ffmpeg"
_QUOTA_NOTICE: Final = "quota is used up"
_RESET_IN: Final = timedelta(hours=1)
# keep: the reset plus the default one-minute margin, so the wait is due at this moment
_PAST_RESET: Final = timedelta(hours=1, minutes=2)
_PR_URL: Final = "https://github.com/acme/shop/pull/7"
_REVIEW_MARKER: Final = "Pull request ready for review"
_ENGINE_DOWN_TEXT: Final = "The engine of api is down."
_ENGINE_DOWN_ID: Final = "api:engine_down:40"
_LAST_EVENT: Final = 40
# keep: a liveness check runs once its interval has passed, which a test moves the clock past for
_LIVENESS_GAP: Final = timedelta(minutes=2)
# keep: a refused restart asks doctor for the failed checks, and this fake is the only command
_DOCTOR_ONLY_SHIKIRU: Final = """#!/bin/sh
case "$*" in
  *doctor*) echo '{"checks": []}' ;;
  *) exit 1 ;;
esac
"""


def _recorded(name: str) -> dict[str, Any]:
    """Return the payload Shikiru gave once for the tool `name`."""
    return json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _listing(reason: str) -> dict[str, Any]:
    """Return the recorded open escalation with its reason changed to `reason`."""
    listing = _recorded("list_escalations")
    listing["result"][0]["escalation"]["reason"] = reason
    return listing


def _exhausted(reset: datetime) -> dict[str, Any]:
    """Return an engine status whose Claude five-hour window is used up until `reset`."""
    window = {"name": "five_hour", "used_pct": 100.0, "resets_at": reset.isoformat()}
    return {"engine_up": True, "quotas": {"claude": [window]}}


def _recorded_event(kind: str, event_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """Return the recorded event with its id, kind and data changed, as Shikiru gives them."""
    event = _recorded("wait_for_events")["result"][0]
    return {**event, "id": event_id, "kind": kind, "message": kind, "data": data}


class _ShikiruSession:
    """Answers the tools of one healthy project from the recorded fixtures, or from `answers`.

    A submit is recorded and answered, unless `hangs_submit` keeps it from being answered, as a
    process that dies inside the call leaves it. A poll is answered with `events` at once, and with
    `later` as well once `gate` is set, as an event that comes after the first ones does. A resolve
    is answered with `resolve_status`: `queued` for a reply, `done` for an install whose answer the
    executor confirms.
    """

    def __init__(
        self,
        resolved: asyncio.Event,
        *,
        hangs_submit: bool = False,
        answers: Mapping[str, dict[str, Any]] | None = None,
        events: Sequence[dict[str, Any]] = (),
        later: Sequence[dict[str, Any]] = (),
        gate: asyncio.Event | None = None,
        resolve_status: str = "queued",
    ) -> None:
        self.resolved = resolved
        self.resolutions: list[dict[str, Any]] = []
        self.submits: list[str] = []
        self.submitted = asyncio.Event()
        self.hangs_submit = hangs_submit
        self._answers: dict[str, dict[str, Any]] = {"status": {"engine_up": True, "quotas": {}}}
        self._answers.update(answers or {})
        self._events = list(events)
        self._later = list(later)
        self._gate = gate
        self._resolve_status = resolve_status

    async def call_tool(self, name: str, arguments: dict[str, Any], *_ignored: object) -> object:
        if name == "wait_for_events":
            events = await self._poll(arguments["since_id"], arguments["timeout_s"])
            payload: dict[str, Any] = {"result": events}
        elif name == "resolve_escalation":
            self.resolutions.append(arguments)
            self.resolved.set()
            payload = {"id": 12, "status": self._resolve_status, "result": None, "message": None}
        elif name == "submit_task":
            self.submits.append(arguments["request"])
            self.submitted.set()
            if self.hangs_submit:
                await asyncio.Event().wait()
            payload = {"id": 7, "status": "done", "result": {"task_id": "T-9"}, "message": None}
        else:
            payload = self._answers[name] if name in self._answers else _recorded(name)
        return CallToolResult(content=[], structuredContent=payload, isError=False)

    async def _poll(self, since_id: int, timeout_s: float) -> list[dict[str, Any]]:
        """Answer the events after `since_id`; a long poll with none waits out its timeout."""
        if timeout_s > 0 and self._gate is not None:
            await self._gate.wait()
        released = self._gate is not None and self._gate.is_set()
        pool = [*self._events, *self._later] if released else self._events
        events = [event for event in pool if event["id"] > since_id]
        if not events and timeout_s > 0:
            await NapClock().sleep(timeout_s)
        return events

    def restore_engine(self) -> None:
        self._answers["status"] = {"engine_up": True, "quotas": {}}


class _InstallRunner:
    """Answers the fake Reasoner and the read-only host probes, and records the one install.

    The install command is recorded in `installs` and answered with success instead of running,
    so no package manager runs. The check finds the tool only once that command has run, as a host
    does after an install.
    """

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.installs: list[str] = []
        self.installed = False

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        del timeout_s, cwd, env
        command = tuple(argv)
        self.commands.append(command)
        if command[0] == "claude":
            answer = {
                "is_error": False,
                "structured_output": {
                    "verdict": "host_tool",
                    "tool": "ffmpeg",
                    "manager": "brew",
                    "package": "ffmpeg",
                    "why": "The build needs ffmpeg.",
                    "suggestion": "Install ffmpeg on this host.",
                },
            }
            return CommandResult(0, json.dumps(answer), "")
        if command[-1] == _INSTALL_COMMAND:
            self.installs.append(command[-1])
            self.installed = True
            return CommandResult(0, "", "")
        script = command[3]
        if "for tool in" in script:
            return CommandResult(0, "os=test\nmanager=brew\npath=/test/bin\n", "")
        if 'if location=$(command -v "$1")' in script:
            if self.installed:
                return CommandResult(0, f"found={_FOUND_PATH}\n", "")
            return CommandResult(0, "missing\n", "")
        message = f"unexpected command in install test: {command[0]}"
        raise AssertionError(message)


def _shikiru_link(
    session: _ShikiruSession, *, delayed: bool = False
) -> Callable[[], AbstractAsyncContextManager[ToolSession]]:
    """Return a link that comes up after one wait when `delayed`, as a starting Shikiru does."""

    @asynccontextmanager
    async def link() -> AsyncGenerator[ToolSession]:
        if delayed:
            await NapClock().sleep(0)
        yield session  # pyright: ignore[reportReturnType]

    return link


def _instance(home: Path, group_id: int | None = None) -> Path:
    component = home / "api"
    component.mkdir()
    directory = home / _PRODUCT
    directory.mkdir()
    group = "" if group_id is None else f"group_id = {group_id}\n"
    (directory / "config.toml").write_text(
        f'product = "{_PRODUCT}"\n\n[[components]]\nname = "api"\npath = "{component}"\n\n'
        f"[channels.telegram]\nowners = [{_OWNER}]\n{group}\n"
        "[operator]\ndispatch_interval_s = 1\noutbox_interval_s = 1\n\n"
        "[handlers.infrastructure]\nrestart_poll_s = 1\n",
        encoding="utf-8",
    )
    return directory


def _spend_restart(directory: Path) -> None:
    # keep: the hour's one automatic restart is used up first, so the engine's restart is refused
    database = Database.open(directory / JOURNAL_FILE_NAME)
    try:
        LoopGuard(JournalActions(OpStore(database)), NapClock(), 1, 1).restart("api")
    finally:
        database.close()


def _interrupted_restart(directory: Path, signal: Signal, new: NewSignal) -> None:
    """Journal a restart of `signal` that was issued and then cut short, with the signal open."""
    now = SystemClock().now()
    database = Database.open(directory / JOURNAL_FILE_NAME)
    try:
        signals = SignalStore(database)
        signals.insert_if_absent(new, now)
        JournalStarts(database, OpStore(database), signals).issue(signal, now)
    finally:
        database.close()


async def _deliver(jobs: Sequence[Job], scenario: Awaitable[None]) -> None:
    """Run the composed jobs while `scenario` plays out, then stop every job."""
    running = [asyncio.create_task(job()) for job in jobs]
    try:
        async with asyncio.timeout(_DELIVERY_TIMEOUT_S):
            await scenario
    finally:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


async def _until(holds: Callable[[], bool]) -> None:
    """Let the composed jobs run until `holds`, which they make true."""
    while not holds():
        await NapClock().sleep(0)


def _health_delivered(directory: Path) -> bool:
    """Say whether the journal records a health message as sent, through a connection of its own."""
    database = Database.open(directory / JOURNAL_FILE_NAME)
    try:
        row = database.connection.execute(
            """
            SELECT COUNT(*) AS sent FROM messages
            JOIN signals ON signals.number = messages.signal_number
            WHERE signals.kind = 'health' AND messages.platform_message_id IS NOT NULL
            """
        ).fetchone()
    finally:
        database.close()
    return int(row["sent"]) > 0


def _edited(bot: FakeBotApi, text: str) -> bool:
    return any(
        method == "editMessageText" and text in str(payload.get("text", ""))
        for method, payload in bot.calls
    )


def _signal_state(directory: Path, signal_id: str) -> SignalState | None:
    """Return the journal's state of the signal, read through a connection of its own."""
    database = Database.open(directory / JOURNAL_FILE_NAME)
    try:
        stored = SignalStore(database).by_id(signal_id)
    finally:
        database.close()
    return None if stored is None else stored.state


class _OutageBot(FakeBotApi):
    """A fake Bot API that refuses getUpdates, as one with a webhook set does, while `refusing`."""

    def __init__(self) -> None:
        super().__init__()
        self.refusing = True

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        if method == "getUpdates" and self.refusing:
            return _CONFLICT
        return await super().call(method, payload)


class _MarkingBot(FakeBotApi):
    """A fake Bot API that also sets `marked` once a message containing `marker` is sent."""

    def __init__(self, marker: str) -> None:
        super().__init__()
        self._marker = marker
        self.marked = asyncio.Event()

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        if self._marker in str(payload.get("text", "")):
            self.marked.set()
        return await super().call(method, payload)


class _ForumBot(FakeBotApi):
    """A fake Bot API that creates each forum topic under the next thread id, from `_TOPIC` on."""

    def __init__(self, updates: list[dict[str, Any]]) -> None:
        super().__init__(updates)
        self._threads = itertools.count(_TOPIC)

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        if method != "createForumTopic":
            return await super().call(method, payload)
        self.calls.append((method, dict(payload)))
        created = json.loads(_FORUM_TOPIC.read_text(encoding="utf-8"))
        created["result"]["message_thread_id"] = next(self._threads)
        return created


class _StagedBot(FakeBotApi):
    """A fake Bot API that gives its updates one at a time, each once one more message was sent.

    The first update waits for the bot's first sent message and the second for its second, as a PO
    who answers a question the bot has just asked does.
    """

    def __init__(self, updates: list[dict[str, Any]]) -> None:
        super().__init__()
        self._staged = updates
        self._given = 0
        self._sent = 0

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        if method == "sendMessage":
            self._sent += 1
        elif method == "getUpdates" and self._given < min(self._sent, len(self._staged)):
            self._updates = [self._staged[self._given]]
            self._given += 1
        return await super().call(method, payload)


class _RestartBot(FakeBotApi):
    """Keeps fake Telegram message ids continuous when a composed service restarts."""

    def __init__(self, updates: list[dict[str, Any]]) -> None:
        super().__init__(updates, after_send=False)
        self._message_ids = itertools.count(FIRST_MESSAGE_ID + 1)


def _in_topic(bot: FakeBotApi, text: str) -> list[dict[str, Any]]:
    """Return the messages the bot sent into the topic that carry `text`."""
    return [
        payload
        for method, payload in bot.calls
        if method == "sendMessage"
        and payload.get("message_thread_id") == _TOPIC
        and text in str(payload.get("text", ""))
    ]


def _install_approvals(bot: FakeBotApi) -> list[dict[str, Any]]:
    return [
        payload
        for method, payload in bot.calls
        if method == "sendMessage" and "Install approval" in str(payload.get("text", ""))
    ]


def _install_state(directory: Path, op_id: int) -> str | None:
    database = Database.open(directory / JOURNAL_FILE_NAME)
    try:
        operation = OpStore(database).install_op(op_id)
    finally:
        database.close()
    return None if operation is None else operation.state


def _deliver_install(
    directory: Path,
    runner: _InstallRunner,
    bot: FakeBotApi,
    session: _ShikiruSession,
    scenario: Awaitable[None],
) -> None:
    composition = compose(
        load_settings(directory),
        directory,
        link=lambda _executable, _project: _shikiru_link(session),
        commands=runner,
        bot=lambda _settings: bot,
        clock=NapClock(),
    )
    try:
        asyncio.run(_deliver(composition.jobs, scenario))
    finally:
        composition.close()


def _initial_install(directory: Path, runner: _InstallRunner) -> tuple[str, int, datetime]:
    with (directory / "config.toml").open("a", encoding="utf-8") as config:
        config.write('\n[reasoner]\nladder = ["claude:haiku"]\n')
    bot = FakeBotApi()
    _deliver_install(
        directory,
        runner,
        bot,
        _ShikiruSession(asyncio.Event()),
        _until(lambda: bool(_install_approvals(bot))),
    )

    [approval] = _install_approvals(bot)
    buttons = [button for row in approval["reply_markup"]["inline_keyboard"] for button in row]
    assert "Tool: ffmpeg" in approval["text"]
    assert "Command: brew install ffmpeg" in approval["text"]
    assert [button["text"] for button in buttons] == ["Install", "Decline"]
    callback_data = buttons[0]["callback_data"]
    _, number, op_id_text = callback_data.split(":")
    op_id = int(op_id_text)
    database = Database.open(directory / JOURNAL_FILE_NAME)
    try:
        signals = SignalStore(database)
        classified = signals.by_id(_E1_ID)
        proposal = signals.install_proposal(_E1_ID)
        operation = OpStore(database).install_op(op_id)
    finally:
        database.close()
    assert classified is not None
    assert (classified.category, classified.classified_by, classified.verdict) == (
        "infrastructure",
        "reasoner",
        "host_tool",
    )
    assert proposal is not None
    assert (proposal.tool, proposal.manager, proposal.package, proposal.why) == (
        "ffmpeg",
        "brew",
        "ffmpeg",
        "The build needs ffmpeg.",
    )
    assert operation is not None
    assert (number, operation.state, operation.payload.argv) == (
        str(operation.payload.number),
        "intended",
        ("brew", "install", "ffmpeg"),
    )
    assert all(
        command[0] not in {"brew", "apt", "dnf", "npm", "pip"} for command in runner.commands
    )
    return callback_data, op_id, operation.created_at


def _open_install(
    directory: Path,
    runner: _InstallRunner,
    callback_data: str,
    op_id: int,
    created_at: datetime,
) -> tuple[str, int]:
    update = json.loads((TELEGRAM_FIXTURES / "get_updates_ok.json").read_text())["result"][0]
    update["update_id"] = 800
    update["message"]["text"] = "/open"
    bot = _RestartBot([update])
    _deliver_install(
        directory,
        runner,
        bot,
        _ShikiruSession(asyncio.Event()),
        _until(lambda: bool(_install_approvals(bot))),
    )

    [approval] = _install_approvals(bot)
    buttons = [button for row in approval["reply_markup"]["inline_keyboard"] for button in row]
    assert buttons[0]["callback_data"] == callback_data
    assert _install_state(directory, op_id) == "intended"
    database = Database.open(directory / JOURNAL_FILE_NAME)
    try:
        operation = OpStore(database).install_op(op_id)
    finally:
        database.close()
    assert operation is not None
    assert operation.created_at == created_at
    assert operation.payload.argv == ("brew", "install", "ffmpeg")
    database = Database.open(directory / JOURNAL_FILE_NAME)
    try:
        row = database.connection.execute(
            "SELECT platform_message_id FROM messages WHERE notice_key = ?",
            (f"install:{op_id}:open:800",),
        ).fetchone()
    finally:
        database.close()
    assert row is not None
    _, _, message_id = row["platform_message_id"].partition("/")
    return buttons[0]["callback_data"], int(message_id)


def _tap_install(
    directory: Path, runner: _InstallRunner, callback_data: str, message_id: int, op_id: int
) -> None:
    update = json.loads((TELEGRAM_FIXTURES / "get_updates_ok.json").read_text())["result"][2]
    update["update_id"] = 801
    update["callback_query"]["from"]["id"] = _OWNER
    update["callback_query"]["message"]["message_id"] = message_id
    update["callback_query"]["data"] = callback_data
    session = _ShikiruSession(asyncio.Event(), resolve_status="done")
    _deliver_install(
        directory,
        runner,
        FakeBotApi([update], after_send=False),
        session,
        _until(lambda: _install_state(directory, op_id) in {"confirmed", "failed"}),
    )
    assert _install_state(directory, op_id) == "confirmed"
    assert runner.installs == [_INSTALL_COMMAND]
    assert session.resolutions == [
        {
            "escalation_id": "E-1",
            "resolution": {
                "kind": "answer",
                "text": f"Installed ffmpeg with brew (found at {_FOUND_PATH}). Please continue.",
            },
        }
    ]


def test_a_source_escalation_ends_as_a_sent_message_with_buttons(tmp_path: Path) -> None:
    directory = _instance(tmp_path)
    bot = FakeBotApi()
    session = _ShikiruSession(asyncio.Event())
    composition = compose(
        load_settings(directory),
        directory,
        link=lambda _executable, _project: _shikiru_link(session),
        bot=lambda _settings: bot,
        clock=NapClock(),
    )
    try:
        asyncio.run(_deliver(composition.jobs, _until(bot.called.is_set)))
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


def test_host_tool_approval_survives_restart_open_and_reply(tmp_path: Path) -> None:
    """An approval that comes after a restart runs the install and confirms it.

    Changed by D6d: this test used to stop at `approved`, because nothing ran the op. The composed
    executor now runs the one stored command, the check finds the tool, and the op ends `confirmed`
    once Shikiru acknowledges the answer as done.
    """
    directory = _instance(tmp_path)
    runner = _InstallRunner()
    callback_data, op_id, created_at = _initial_install(directory, runner)
    copy_data, message_id = _open_install(directory, runner, callback_data, op_id, created_at)
    _tap_install(directory, runner, copy_data, message_id, op_id)


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
        clock=NapClock(),
    )
    try:
        asyncio.run(_deliver(composition.jobs, _until(session.resolved.is_set)))
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


def test_help_in_a_component_topic_is_answered_in_that_topic(tmp_path: Path) -> None:
    directory = _instance(tmp_path, group_id=_GROUP)
    updates = json.loads((TELEGRAM_FIXTURES / "get_updates_ok.json").read_text(encoding="utf-8"))
    command = updates["result"][0]
    command["message"].update(
        chat={"id": _GROUP},
        message_thread_id=_TOPIC,
        reply_to_message={"message_id": _TOPIC},
        text="/help",
    )
    bot = _ForumBot([command])
    session = _ShikiruSession(asyncio.Event())
    composition = compose(
        load_settings(directory),
        directory,
        link=lambda _executable, _project: _shikiru_link(session),
        bot=lambda _settings: bot,
        clock=NapClock(),
    )
    try:
        composition.recover()
        asyncio.run(_deliver(composition.jobs, _until(lambda: bool(_in_topic(bot, _HELP_LINE)))))
    finally:
        composition.close()

    [answer] = _in_topic(bot, _HELP_LINE)
    assert answer["chat_id"] == str(_GROUP)


def test_a_reply_to_the_prompt_resolves_the_escalation_with_its_text(tmp_path: Path) -> None:
    directory = _instance(tmp_path)
    updates = json.loads((TELEGRAM_FIXTURES / "get_updates_ok.json").read_text(encoding="utf-8"))
    reply, tap = updates["result"][1], updates["result"][2]
    reply["message"]["reply_to_message"]["message_id"] = _PROMPT_ID
    tap["callback_query"]["data"] = "a:1:3"
    bot = _StagedBot([tap, reply])
    session = _ShikiruSession(asyncio.Event())
    composition = compose(
        load_settings(directory),
        directory,
        link=lambda _executable, _project: _shikiru_link(session),
        bot=lambda _settings: bot,
        clock=NapClock(),
    )
    try:
        asyncio.run(_deliver(composition.jobs, _until(session.resolved.is_set)))
    finally:
        composition.close()

    assert session.resolutions == [
        {
            "escalation_id": "E-1",
            "resolution": {"kind": "answer", "text": "Use the second way"},
        }
    ]
    [prompt] = [
        payload
        for method, payload in bot.calls
        if method == "sendMessage" and "Reply with your answer" in str(payload.get("text", ""))
    ]
    assert prompt["reply_markup"] == {"force_reply": True, "input_field_placeholder": "Your answer"}


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
        clock=NapClock(),
    )
    try:
        asyncio.run(_deliver(composition.jobs, _until(crashed.submitted.is_set)))
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
    bot = _MarkingBot(_ASKED)
    composition = compose(
        load_settings(directory),
        directory,
        link=lambda _executable, _project: _shikiru_link(restarted),
        bot=lambda _settings: bot,
        clock=NapClock(),
    )
    try:
        composition.recover()
        asyncio.run(_deliver(composition.jobs, _until(bot.marked.is_set)))
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


def test_an_interrupted_start_is_settled_before_its_signal_is_handled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # keep: no command can run here, so a start or doctor that should not happen fails instead
    monkeypatch.setenv("PATH", str(tmp_path))
    directory = _instance(tmp_path)
    _interrupted_restart(directory, _ENGINE_DOWN, _ENGINE_DOWN_NEW)
    session = _ShikiruSession(asyncio.Event())
    bot = _MarkingBot(_RESTARTED)
    composition = compose(
        load_settings(directory),
        directory,
        link=lambda _executable, _project: _shikiru_link(session, delayed=True),
        bot=lambda _settings: bot,
        clock=NapClock(),
    )
    try:
        asyncio.run(_deliver(composition.jobs, _until(bot.marked.is_set)))
    finally:
        composition.close()

    texts = [
        str(payload.get("text", "")) for method, payload in bot.calls if method == "sendMessage"
    ]
    assert not [text for text in texts if _FAILED_HANDLER in text]
    database = Database.open(directory / JOURNAL_FILE_NAME)
    try:
        signals = SignalStore(database)
        attempt = JournalStarts(database, OpStore(database), signals).existing(_ENGINE_DOWN)
        row = database.connection.execute(
            "SELECT COUNT(*) AS notices FROM messages WHERE notice_key = ?",
            (f"restart:{_ENGINE_DOWN.id}",),
        ).fetchone()
    finally:
        database.close()
    assert attempt is not None
    assert (attempt.state, row["notices"]) == (AttemptState.CONFIRMED, 1)


def test_a_removed_component_neither_stops_recovery_nor_holds_back_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # keep: no command can run here, so a start of the removed component fails instead
    monkeypatch.setenv("PATH", str(tmp_path))
    directory = _instance(tmp_path)
    _interrupted_restart(directory, _ENGINE_DOWN, _ENGINE_DOWN_NEW)
    _interrupted_restart(directory, _REMOVED_DOWN, _REMOVED_DOWN_NEW)
    session = _ShikiruSession(asyncio.Event())
    bot = _MarkingBot(_RESTARTED)
    composition = compose(
        load_settings(directory),
        directory,
        link=lambda _executable, _project: _shikiru_link(session, delayed=True),
        bot=lambda _settings: bot,
        clock=NapClock(),
    )
    try:
        composition.recover()
        asyncio.run(_deliver(composition.jobs, _until(bot.marked.is_set)))
    finally:
        composition.close()

    database = Database.open(directory / JOURNAL_FILE_NAME)
    try:
        signals = SignalStore(database)
        removed = signals.by_id(_REMOVED_DOWN.id)
        attempt = JournalStarts(database, OpStore(database), signals).existing(_REMOVED_DOWN)
        row = database.connection.execute(
            "SELECT COUNT(*) AS notices FROM messages WHERE notice_key = ?",
            (f"restart:{_ENGINE_DOWN.id}",),
        ).fetchone()
    finally:
        database.close()
    assert removed is not None
    assert (removed.state, removed.notes) == (SignalState.CLOSED, REMOVED_NOTE)
    assert attempt is not None
    assert (attempt.state, row["notices"]) == (AttemptState.ISSUED, 1)


def test_a_retry_is_given_back_to_shikiru_once_and_the_po_gets_no_message(tmp_path: Path) -> None:
    directory = _instance(tmp_path)
    bot = FakeBotApi()
    session = _ShikiruSession(asyncio.Event(), answers={"list_escalations": _listing(AGENT_ERROR)})
    composition = compose(
        load_settings(directory),
        directory,
        link=lambda _executable, _project: _shikiru_link(session),
        bot=lambda _settings: bot,
        clock=NapClock(),
    )
    resolved = _until(lambda: _signal_state(directory, _E1_ID) == SignalState.RESOLVED)
    try:
        asyncio.run(_deliver(composition.jobs, resolved))
    finally:
        composition.close()

    retries = [(call["escalation_id"], call["resolution"]["kind"]) for call in session.resolutions]
    assert retries == [("E-1", ResolutionKind.RETRY)]
    assert [method for method, _payload in bot.calls if method == "sendMessage"] == []


async def _quota_cycle(
    directory: Path, bot: _MarkingBot, session: _ShikiruSession, clock: NapClock
) -> None:
    """Wait until the PO is told and the escalation is parked, then move the clock past the reset.

    Only the move makes the wait due, so the retry comes after it and not before.
    """
    await _until(
        lambda: bot.marked.is_set() and _signal_state(directory, _E1_ID) == SignalState.PARKED
    )
    assert session.resolutions == []
    clock.advance(_PAST_RESET)
    await _until(lambda: _signal_state(directory, _E1_ID) == SignalState.RESOLVED)


def test_a_quota_wait_tells_the_po_then_retries_once_after_the_reset(tmp_path: Path) -> None:
    directory = _instance(tmp_path)
    clock = NapClock()
    session = _ShikiruSession(
        asyncio.Event(),
        answers={
            "list_escalations": _listing(QUOTA_EXHAUSTED),
            "status": _exhausted(clock.now() + _RESET_IN),
        },
    )
    bot = _MarkingBot(_QUOTA_NOTICE)
    composition = compose(
        load_settings(directory),
        directory,
        link=lambda _executable, _project: _shikiru_link(session),
        bot=lambda _settings: bot,
        clock=clock,
    )
    try:
        asyncio.run(_deliver(composition.jobs, _quota_cycle(directory, bot, session, clock)))
    finally:
        composition.close()

    retries = [(call["escalation_id"], call["resolution"]["kind"]) for call in session.resolutions]
    assert retries == [("E-1", ResolutionKind.RETRY)]
    [notice] = [payload for method, payload in bot.calls if method == "sendMessage"]
    assert _QUOTA_NOTICE in str(notice["text"])


def test_a_pull_request_opened_is_sent_for_review_and_the_task_done_closes_its_message(
    tmp_path: Path,
) -> None:
    directory = _instance(tmp_path)
    bot = _MarkingBot(_REVIEW_MARKER)
    session = _ShikiruSession(
        asyncio.Event(),
        answers={"list_escalations": {"result": []}},
        events=[_recorded_event(EventKinds.PR_OPENED, 41, {"url": _PR_URL, "commits": 2})],
        later=[_recorded_event(EventKinds.TASK_DONE, 42, {})],
        gate=bot.marked,
    )
    composition = compose(
        load_settings(directory),
        directory,
        link=lambda _executable, _project: _shikiru_link(session),
        bot=lambda _settings: bot,
        clock=NapClock(),
    )
    edited = _until(lambda: any(method == "editMessageText" for method, _ in bot.calls))
    try:
        asyncio.run(_deliver(composition.jobs, edited))
    finally:
        composition.close()

    [review] = [payload for method, payload in bot.calls if method == "sendMessage"]
    [[open_pr], [acknowledge]] = review["reply_markup"]["inline_keyboard"]
    assert (open_pr["url"], acknowledge["callback_data"].startswith("k:")) == (_PR_URL, True)
    [edit] = [payload for method, payload in bot.calls if method == "editMessageText"]
    final = (edit["message_id"], edit["reply_markup"], MERGED_LINE in str(edit["text"]))
    assert final == (FIRST_MESSAGE_ID, {"inline_keyboard": []}, True)


def test_an_operator_health_message_is_edited_to_recovered_after_a_restart(
    tmp_path: Path,
) -> None:
    directory = _instance(tmp_path)
    bot = _OutageBot()
    first = compose(
        load_settings(directory),
        directory,
        link=lambda _executable, _project: _shikiru_link(_ShikiruSession(asyncio.Event())),
        bot=lambda _settings: bot,
        clock=NapClock(),
    )
    try:
        asyncio.run(_deliver(first.jobs, _until(lambda: _health_delivered(directory))))
    finally:
        first.close()

    bot.refusing = False
    restarted = compose(
        load_settings(directory),
        directory,
        link=lambda _executable, _project: _shikiru_link(_ShikiruSession(asyncio.Event())),
        bot=lambda _settings: bot,
        clock=NapClock(),
    )
    try:
        restarted.recover()
        asyncio.run(_deliver(restarted.jobs, _until(lambda: _edited(bot, RECOVERED_LINE))))
    finally:
        restarted.close()

    [edit] = [payload for method, payload in bot.calls if method == "editMessageText"]
    assert str(edit["text"]).endswith(RECOVERED_LINE)


def test_an_engine_down_forwarded_for_want_of_a_restart_closes_once_the_engine_is_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shikiru = bin_dir / "shikiru"
    shikiru.write_text(_DOCTOR_ONLY_SHIKIRU, encoding="utf-8")
    shikiru.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    directory = _instance(tmp_path)
    _spend_restart(directory)
    session = _ShikiruSession(
        asyncio.Event(),
        answers={"status": {"engine_up": False, "quotas": {}}},
        events=[_recorded_event(EventKinds.ENGINE_STARTED, _LAST_EVENT, {})],
    )
    bot = _MarkingBot(_ENGINE_DOWN_TEXT)
    clock = NapClock()
    composition = compose(
        load_settings(directory),
        directory,
        link=lambda _executable, _project: _shikiru_link(session),
        bot=lambda _settings: bot,
        clock=clock,
    )

    async def engine_comes_back() -> None:
        await bot.marked.wait()
        session.restore_engine()
        clock.advance(_LIVENESS_GAP)
        await _until(lambda: _edited(bot, ENGINE_UP_LINE))

    try:
        asyncio.run(_deliver(composition.jobs, engine_comes_back()))
    finally:
        composition.close()

    assert _signal_state(directory, _ENGINE_DOWN_ID) is SignalState.CLOSED
    [edit] = [payload for method, payload in bot.calls if method == "editMessageText"]
    final = (str(edit["text"]).endswith(ENGINE_UP_LINE), edit["reply_markup"])
    assert final == (True, {"inline_keyboard": []})
