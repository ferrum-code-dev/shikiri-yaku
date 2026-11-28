"""The core works with a channel whose identifiers have no platform shape."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from mcp.types import CallToolResult, ListToolsResult, Tool

from shikiri_yaku import bootstrap
from shikiri_yaku.adapters.shikiru_mcp import DECLARED_TOOLS
from shikiri_yaku.domain.inbound import InboundKind, Received
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.journal import Delivery
from shikiri_yaku.journal.messages import MessageStore
from shikiri_yaku.journal.outbox import JournalOutbox
from shikiri_yaku.ports import Channel, CommandResult, Message
from shikiri_yaku.settings import load_settings
from tests.support.clock import NapClock
from tests.support.generic_channel import (
    MAIN_PLACE,
    OWNER,
    GenericChannel,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence

    import pytest

    from shikiri_yaku.adapters.shikiru_mcp import Connector, ToolSession
    from shikiri_yaku.cli import Job
    from shikiri_yaku.ports import Deleter

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "shikiru"
_SECRET = "sk-ABCDEFGHIJKLMNOPQRSTUV"  # noqa: S105 -- harmless value used to test secret refusal
_WAIT_TIMEOUT_S = 10
_WAIT_POLL_S = 0.001


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class _Session:
    def __init__(self, clock: NapClock) -> None:
        self._clock = clock
        self.events = [_fixture("wait_for_events")["result"][0]]
        self.resolutions: list[dict[str, Any]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any], *_ignored: object) -> object:
        if name == "wait_for_events":
            cursor = arguments["since_id"] or 0
            result = [event for event in self.events if event["id"] > cursor]
            if not result:
                await self._clock.sleep(arguments["timeout_s"])
            payload: dict[str, Any] = {"result": result}
        elif name == "resolve_escalation":
            self.resolutions.append(arguments)
            payload = {"id": 12, "status": "queued", "result": None, "message": None}
        elif name == "status":
            payload = {"engine_up": True, "quotas": {}}
        else:
            payload = _fixture(name)
        return CallToolResult(content=[], structuredContent=payload, isError=False)

    async def list_tools(self) -> ListToolsResult:
        return ListToolsResult(
            tools=[Tool(name=name, inputSchema={"type": "object"}) for name in DECLARED_TOOLS]
        )


class _AgentRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> CommandResult:
        del timeout_s, cwd, env, stdin
        self.calls.append(list(argv))
        answer: dict[str, Any] = {
            "is_error": False,
            "session_id": "agent/session/one",
            "structured_output": {
                "reply": "Ready",
                "title": "A small answer",
                "tasks": [],
                "findings": "",
            },
        }
        return CommandResult(0, json.dumps(answer), "")


class _Reporter:
    def report(self, line: str) -> None:
        raise AssertionError(line)


class _CountingDeleter:
    """Records each deletion the core asks for, then gives the fake channel's answer."""

    def __init__(self, channel: GenericChannel) -> None:
        self._channel = channel
        self.calls: list[str] = []

    async def delete(self, message_ref: str) -> None:
        self.calls.append(message_ref)
        await self._channel.transport.delete(message_ref)


def _delivery_of(composition: bootstrap.Composition, event_key: str) -> str | None:
    row = composition.journal.connection.execute(
        "SELECT delivery FROM messages WHERE event_key = ?", (event_key,)
    ).fetchone()
    return None if row is None else str(row["delivery"])


def _directory(root: Path) -> Path:
    component = root / "component"
    component.mkdir()
    instance = root / "operator"
    instance.mkdir()
    (instance / "config.toml").write_text(
        'product = "shop"\n\n'
        '[[components]]\nname = "api"\npath = "'
        + str(component)
        + '"\n\n[channels.telegram]\nowners = [1001]\n\n'
        '[expert]\nladder = ["claude:haiku"]\n',
        encoding="utf-8",
    )
    return instance


def _link(session: _Session) -> Callable[[str, Path], Connector]:
    def connect(_executable: str, _project: Path) -> Connector:
        @asynccontextmanager
        async def opened() -> AsyncGenerator[ToolSession]:
            yield cast("ToolSession", session)

        return opened

    return connect


async def _until(clock: NapClock, predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(_WAIT_TIMEOUT_S):
        while not predicate():
            await clock.sleep(_WAIT_POLL_S)


async def _drive(jobs: Sequence[Job], scenario: Awaitable[None]) -> None:
    tasks = [asyncio.create_task(job()) for job in jobs]
    try:
        await scenario
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _matching(
    channel: GenericChannel, text: str, *, place_id: str | None = None
) -> list[tuple[str, Message]]:
    return [
        (reference, message)
        for reference, (place, message) in channel.transport.sent.items()
        if text in message.text and (place_id is None or place.id == place_id)
    ]


def _build_channel(
    channel: GenericChannel, deleter: Deleter | None = None
) -> Callable[..., Channel]:
    def build_channel(*_args: object) -> Channel:
        return channel.bundle if deleter is None else replace(channel.bundle, deleter=deleter)

    return build_channel


def _compose(
    directory: Path,
    session: _Session,
    runner: _AgentRunner,
    clock: NapClock,
) -> bootstrap.Composition:
    return bootstrap.compose(
        load_settings(directory),
        directory,
        link=_link(session),
        commands=runner,
        clock=clock,
    )


def test_opaque_channel_forwards_answers_and_runs_an_expert_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _directory(tmp_path)
    channel = GenericChannel()
    clock = NapClock()
    session = _Session(clock)
    runner = _AgentRunner()
    monkeypatch.setattr(bootstrap, "build_channel", _build_channel(channel))
    composition = _compose(directory, session, runner, clock)
    try:
        composition.recover()

        async def scenario() -> None:
            await _until(clock, lambda: bool(_matching(channel, "Which behavior should remain?")))
            [question] = _matching(channel, "Which behavior should remain?")
            question_place = next(
                place
                for reference, (place, _message) in channel.transport.sent.items()
                if reference == question[0]
            )
            _question_ref, question_message = question
            assert question_place.id == "place:component:api"
            assert not question_place.id.isdecimal()
            channel.inbox.add(
                Received(
                    key="event/answer/one",
                    kind=InboundKind.TAP,
                    sender=OWNER,
                    place=question_place,
                    message_ref=question[0],
                    data=question_message.buttons[0].data,
                )
            )
            await _until(clock, lambda: bool(session.resolutions))

            channel.inbox.add(
                Received(
                    key="event/expert/open",
                    kind=InboundKind.TEXT,
                    sender=OWNER,
                    place=MAIN_PLACE,
                    message_ref="owner/message/open",
                    text="/expert Explain this behavior",
                )
            )
            room = "room/opaque/alpha-1"
            rooms = channel.room_support
            assert rooms is not None
            room_place = rooms.place(room)
            await _until(clock, lambda: bool(_matching(channel, "Ready", place_id=room_place.id)))
            [opened] = _matching(channel, "Open the Expert conversation.")
            assert [button.label for button in opened[1].buttons] == ["Open conversation"]
            assert room_place.id == f"place:conversation:{room}"
            assert not room_place.id.isdecimal()

            channel.inbox.add(
                Received(
                    key="event/expert/followup",
                    kind=InboundKind.TEXT,
                    sender=OWNER,
                    place=room_place,
                    message_ref="owner/message/followup",
                    text="What about the next step?",
                )
            )
            await _until(clock, lambda: len(runner.calls) >= 2)
            channel.inbox.add(
                Received(
                    key="event/expert/close",
                    kind=InboundKind.TEXT,
                    sender=OWNER,
                    place=room_place,
                    message_ref="owner/message/close",
                    text="/close",
                )
            )
            await _until(
                clock,
                lambda: (
                    not composition.journal.connection.execute(
                        "SELECT 1 FROM conversations LIMIT 1"
                    ).fetchone()
                    and room in rooms.deleted
                ),
            )

            outbox = JournalOutbox(
                composition.journal,
                MessageStore(composition.journal, _Reporter()),
            )
            outbox.queue(
                None,
                Purpose.EXPERT,
                [MAIN_PLACE],
                Message(_SECRET),
                key="secret/refusal",
                now=composition.clock.now(),
            )
            await _until(clock, lambda: outbox.settled("secret/refusal"))
            row = composition.journal.connection.execute(
                "SELECT delivery, body FROM messages WHERE notice_key = ?",
                ("secret/refusal",),
            ).fetchone()
            assert row["delivery"] == "refused"
            assert row["body"] is None
            assert not any(
                _SECRET in message.text for _, message in channel.transport.sent.values()
            )

        asyncio.run(_drive(composition.jobs, scenario()))
    finally:
        composition.close()


def test_channel_without_rooms_or_shared_chat_refuses_expert_and_serves_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _directory(tmp_path)
    channel = GenericChannel(rooms=False, shared=False)
    clock = NapClock()
    session = _Session(clock)
    monkeypatch.setattr(bootstrap, "build_channel", _build_channel(channel))
    composition = _compose(directory, session, _AgentRunner(), clock)
    try:
        composition.recover()

        async def scenario() -> None:
            channel.inbox.add(
                Received(
                    key="event/expert/unsupported",
                    kind=InboundKind.TEXT,
                    sender=OWNER,
                    place=MAIN_PLACE,
                    message_ref="owner/message/unsupported",
                    text="/expert Explain this behavior",
                )
            )
            await _until(
                clock,
                lambda: (
                    bool(
                        _matching(
                            channel, "The Expert needs a channel that can open conversations."
                        )
                    )
                    and bool(_matching(channel, "Which behavior should remain?"))
                ),
            )
            [signal] = _matching(channel, "Which behavior should remain?")
            signal_place = next(
                place.id
                for reference, (place, _message) in channel.transport.sent.items()
                if reference == signal[0]
            )
            assert signal_place == "place:component:api"

        asyncio.run(_drive(composition.jobs, scenario()))
    finally:
        composition.close()


def test_an_owner_secret_is_refused_and_its_refused_deletion_is_not_tried_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _directory(tmp_path)
    channel = GenericChannel()
    clock = NapClock()
    session = _Session(clock)
    deleter = _CountingDeleter(channel)
    monkeypatch.setattr(bootstrap, "build_channel", _build_channel(channel, deleter))
    composition = _compose(directory, session, _AgentRunner(), clock)
    try:
        composition.recover()

        async def scenario() -> None:
            channel.inbox.add(
                Received(
                    key="event/secret/owner",
                    kind=InboundKind.TEXT,
                    sender=OWNER,
                    place=MAIN_PLACE,
                    message_ref="owner/message/secret",
                    text=f"the key is {_SECRET}",
                )
            )
            await _until(clock, lambda: len(deleter.calls) == 1)
            channel.inbox.add(
                Received(
                    key="event/later/help",
                    kind=InboundKind.TEXT,
                    sender=OWNER,
                    place=MAIN_PLACE,
                    message_ref="owner/message/help",
                    text="/help",
                )
            )
            await _until(
                clock, lambda: _delivery_of(composition, "event/later/help") == Delivery.HANDLED
            )

            assert deleter.calls == ["owner/message/secret"]
            row = composition.journal.connection.execute(
                "SELECT delivery, body, data, refusal FROM messages WHERE event_key = ?",
                ("event/secret/owner",),
            ).fetchone()
            assert row["delivery"] == Delivery.DELETE_ATTEMPTED
            assert row["body"] is None
            assert row["data"] is None
            assert row["refusal"] is not None
            stored = composition.journal.connection.execute("SELECT * FROM messages").fetchall()
            assert not any(_SECRET in str(value) for stored_row in stored for value in stored_row)
            assert not any(
                _SECRET in message.text for _, message in channel.transport.sent.values()
            )

        asyncio.run(_drive(composition.jobs, scenario()))
    finally:
        composition.close()
