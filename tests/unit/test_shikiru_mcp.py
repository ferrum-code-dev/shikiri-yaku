"""The Shikiru MCP client against a fake session and payloads recorded from Shikiru's S1 branch."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest
from mcp.types import CallToolResult, TextContent

from shikiri_yaku.adapters.shikiru_mcp import ShikiruMcpClient
from shikiri_yaku.ports import (
    ShikiruLinkDownError,
    ShikiruProtocolError,
    ShikiruRejectedError,
    TaskStatus,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable
    from datetime import timedelta

    from shikiri_yaku.adapters.shikiru_mcp import ToolSession

_FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "shikiru"
_BACKOFF_INITIAL_S: Final = 1
_BACKOFF_MAX_S: Final = 5
_CALL_TIMEOUT_S: Final = 30


def _recorded(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _reply(structured: object, *, is_error: bool = False, text: str = "") -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=structured,  # pyright: ignore[reportArgumentType]
        isError=is_error,
    )


class FakeSession:
    def __init__(self, replies: dict[str, CallToolResult] | None = None) -> None:
        self.replies = replies or {}
        self.calls: list[tuple[str, dict[str, Any], timedelta]] = []
        self.broken = False

    async def call_tool(
        self, name: str, arguments: dict[str, Any], read_timeout_seconds: timedelta
    ) -> CallToolResult:
        self.calls.append((name, arguments, read_timeout_seconds))
        if self.broken:
            message = "the server process is gone"
            raise ConnectionError(message)
        return self.replies[name]


class FakeLink:
    """Hands out one session per connection and refuses the first `failures` attempts."""

    def __init__(self, sessions: list[FakeSession], failures: int = 0) -> None:
        self.sessions = sessions
        self.failures = failures
        self.attempts = 0
        self.connected = asyncio.Event()

    def __call__(self) -> Any:
        return self._connect()

    @asynccontextmanager
    async def _connect(self) -> AsyncGenerator[ToolSession]:
        self.attempts += 1
        if self.attempts <= self.failures:
            message = "cannot start shikiru"
            raise OSError(message)
        self.connected.set()
        yield self.sessions[self.attempts - self.failures - 1]


class FakeClock:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return datetime(2026, 10, 28, tzinfo=UTC)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _client(link: FakeLink, clock: FakeClock) -> ShikiruMcpClient:
    return ShikiruMcpClient(
        link,
        clock,
        backoff_initial_s=_BACKOFF_INITIAL_S,
        backoff_max_s=_BACKOFF_MAX_S,
        call_timeout_s=_CALL_TIMEOUT_S,
    )


async def _with_link(
    session: FakeSession, scenario: Callable[[ShikiruMcpClient], Awaitable[None]]
) -> None:
    link = FakeLink([session])
    client = _client(link, FakeClock())
    supervisor = asyncio.create_task(client.supervise())
    try:
        await link.connected.wait()
        await scenario(client)
    finally:
        supervisor.cancel()


def _run(session: FakeSession, scenario: Callable[[ShikiruMcpClient], Awaitable[None]]) -> None:
    asyncio.run(_with_link(session, scenario))


def test_the_s1_escalation_payload_is_read_with_its_decision_fields() -> None:
    session = FakeSession(
        {
            "get_escalation": _reply(_recorded("get_escalation")),
            "list_escalations": _reply(_recorded("list_escalations")),
        }
    )

    async def scenario(client: ShikiruMcpClient) -> None:
        one = await client.get_escalation("E-1")
        listed = await client.list_escalations()
        assert one is not None
        assert listed == [one]
        assert (one.escalation.id, one.escalation.task_id, one.escalation.status) == (
            "E-1",
            "T-1",
            "open",
        )
        assert one.escalation.reason == "decision_required"
        assert one.escalation.resolution is None
        assert one.question == "Which behavior should remain?"
        assert [(o.label, o.recommended, o.needs_input) for o in one.agent_options] == [
            ("Keep existing behavior", True, False),
            ("Change behavior", False, False),
        ]
        assert [(r.kind, r.recommended) for r in one.resolutions] == [
            ("answer", True),
            ("cancel", False),
        ]
        assert one.packet is not None
        assert "## Question" in one.packet

    _run(session, scenario)


def test_status_events_and_tasks_are_read() -> None:
    session = FakeSession(
        {
            "status": _reply(_recorded("status")),
            "wait_for_events": _reply(_recorded("wait_for_events")),
            "list_tasks": _reply(_recorded("list_tasks")),
            "get_task": _reply(_recorded("get_task")),
        }
    )

    async def scenario(client: ShikiruMcpClient) -> None:
        status = await client.status()
        assert status.engine_up is False
        window = status.quotas["claude"][0]
        assert (window.used_pct, window.resets_at) == (
            42.5,
            datetime(2026, 10, 28, 13, tzinfo=UTC),
        )

        events = await client.wait_for_events(40, 25)
        assert [(e.id, e.kind, e.task_id) for e in events] == [
            (41, "escalation", "T-1"),
            (42, "engine_stopped", None),
        ]
        assert events[0].data == {"escalation_id": "E-1", "reason": "decision_required"}

        (summary,) = await client.list_tasks([TaskStatus.ESCALATED])
        detail = await client.get_task("T-1")
        assert detail is not None
        assert detail.task == summary
        assert (summary.id, summary.parent_id, summary.status, summary.title) == (
            "T-1",
            None,
            TaskStatus.ESCALATED,
            "decide",
        )

    _run(session, scenario)

    names = [name for name, _, _ in session.calls]
    assert names == ["status", "wait_for_events", "list_tasks", "get_task"]
    assert session.calls[1][1] == {"since_id": 40, "timeout_s": 25}
    assert session.calls[1][2].total_seconds() == 25 + _CALL_TIMEOUT_S
    assert session.calls[2][1] == {"statuses": ["escalated"]}


def test_an_unknown_escalation_or_task_is_none() -> None:
    session = FakeSession(
        {"get_escalation": _reply({"result": None}), "get_task": _reply({"result": None})}
    )

    async def scenario(client: ShikiruMcpClient) -> None:
        assert await client.get_escalation("E-9") is None
        assert await client.get_task("T-9") is None

    _run(session, scenario)


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(_reply(None), id="no structured content"),
        pytest.param(_reply({"engine_up": "maybe"}), id="wrong type"),
        pytest.param(_reply({"quotas": {}}), id="missing field"),
    ],
)
def test_an_unreadable_result_is_a_protocol_error(bad: CallToolResult) -> None:
    async def scenario(client: ShikiruMcpClient) -> None:
        with pytest.raises(ShikiruProtocolError):
            await client.status()

    _run(FakeSession({"status": bad}), scenario)


def test_a_tool_error_is_a_rejection_with_shikiru_s_message() -> None:
    refused = _reply(None, is_error=True, text="escalation E-1 is already resolved")

    async def scenario(client: ShikiruMcpClient) -> None:
        with pytest.raises(ShikiruRejectedError, match="already resolved"):
            await client.get_escalation("E-1")

    _run(FakeSession({"get_escalation": refused}), scenario)


def test_every_verb_raises_link_down_before_the_link_is_up() -> None:
    client = _client(FakeLink([]), FakeClock())

    async def scenario() -> None:
        with pytest.raises(ShikiruLinkDownError):
            await client.status()
        with pytest.raises(ShikiruLinkDownError):
            await client.wait_for_events(0, 1)

    asyncio.run(scenario())


def test_a_lost_link_raises_link_down_until_it_reconnects() -> None:
    first = FakeSession()
    second = FakeSession({"status": _reply({"engine_up": True})})
    link = FakeLink([first, second])
    clock = FakeClock()
    client = _client(link, clock)

    async def scenario() -> None:
        supervisor = asyncio.create_task(client.supervise())
        try:
            await link.connected.wait()
            link.connected.clear()
            first.broken = True
            with pytest.raises(ShikiruLinkDownError, match="gone"):
                await client.status()
            with pytest.raises(ShikiruLinkDownError):
                await client.status()
            assert len(first.calls) == 1
            await link.connected.wait()
            assert (await client.status()).engine_up is True
        finally:
            supervisor.cancel()

    asyncio.run(scenario())
    assert clock.sleeps == [_BACKOFF_INITIAL_S]


def test_reconnecting_waits_longer_after_each_failure_up_to_a_limit() -> None:
    failures = 5
    link = FakeLink([FakeSession()], failures=failures)
    clock = FakeClock()
    client = _client(link, clock)

    async def scenario() -> None:
        supervisor = asyncio.create_task(client.supervise())
        try:
            await link.connected.wait()
        finally:
            supervisor.cancel()

    asyncio.run(scenario())
    assert clock.sleeps == [1, 2, 4, 5, 5]
