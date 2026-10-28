"""The ShikiruClient over one project's `shikiru --project <path> mcp` on stdio (design §11)."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent
from pydantic import BaseModel, ConfigDict, ValidationError

from shikiri_yaku.ports import (
    Ack,
    AckStatus,
    EngineStatus,
    EscalationView,
    ShikiruError,
    ShikiruEvent,
    ShikiruLinkDownError,
    ShikiruProtocolError,
    ShikiruRejectedError,
    ShikiruTask,
    TaskDetail,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Sequence
    from pathlib import Path

    from mcp.types import CallToolResult

    from shikiri_yaku.ports import Clock, CommandRunner

BACKOFF_FACTOR = 2


class ToolSession(Protocol):
    """The one call of an initialized MCP session that the client uses."""

    async def call_tool(
        self, name: str, arguments: dict[str, Any], read_timeout_seconds: timedelta
    ) -> CallToolResult: ...


type Connector = Callable[[], AbstractAsyncContextManager[ToolSession]]


class _Result[T](BaseModel):
    """Shikiru's server wraps a list or optional result in a `result` key."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    result: T


def stdio_connector(executable: str, project: Path) -> Connector:
    """Connect to `executable --project <project> mcp`; the executable is found on PATH."""
    parameters = StdioServerParameters(command=executable, args=["--project", str(project), "mcp"])

    @asynccontextmanager
    async def connect() -> AsyncGenerator[ToolSession]:
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            yield session

    return connect


class ShikiruMcpClient:
    """Talks to one project's Shikiru. `supervise` keeps the link up while it runs.

    A call that fails on the transport drops the link at once, so the next verbs raise
    ShikiruLinkDownError until `supervise` has reconnected.
    """

    def __init__(
        self,
        connect: Connector,
        clock: Clock,
        *,
        backoff_initial_s: float,
        backoff_max_s: float,
        call_timeout_s: float,
        runner: CommandRunner,
        start_argv: Sequence[str],
        start_timeout_s: float,
    ) -> None:
        self._connect = connect
        self._clock = clock
        self._runner = runner
        self._start_argv = list(start_argv)
        self._start_timeout_s = start_timeout_s
        self._backoff_initial_s = backoff_initial_s
        self._backoff_max_s = backoff_max_s
        self._call_timeout_s = call_timeout_s
        self._session: ToolSession | None = None
        self._dropped = asyncio.Event()

    async def supervise(self) -> None:
        """Keep the link up, reconnecting after growing pauses, until cancelled."""
        delay_s = self._backoff_initial_s
        while True:
            try:
                # keep: any failure to connect or to hold the link is retried after a pause
                with suppress(Exception):
                    async with self._connect() as session:
                        self._session = session
                        self._dropped.clear()
                        delay_s = self._backoff_initial_s
                        await self._dropped.wait()
            finally:
                self._session = None
            await self._clock.sleep(delay_s)
            delay_s = min(delay_s * BACKOFF_FACTOR, self._backoff_max_s)

    async def wait_for_events(self, since_id: int, timeout_s: float) -> list[ShikiruEvent]:
        arguments = {"since_id": since_id, "timeout_s": timeout_s}
        read_timeout_s = timeout_s + self._call_timeout_s
        reply = await self._call(
            "wait_for_events", arguments, _Result[list[ShikiruEvent]], read_timeout_s
        )
        return reply.result

    async def status(self) -> EngineStatus:
        return await self._call("status", {}, EngineStatus)

    async def list_escalations(self) -> list[EscalationView]:
        return (await self._call("list_escalations", {}, _Result[list[EscalationView]])).result

    async def get_escalation(self, escalation_id: str) -> EscalationView | None:
        arguments = {"escalation_id": escalation_id}
        return (
            await self._call("get_escalation", arguments, _Result[EscalationView | None])
        ).result

    async def list_tasks(self, statuses: Sequence[str] | None = None) -> list[ShikiruTask]:
        arguments = {"statuses": None if statuses is None else list(statuses)}
        return (await self._call("list_tasks", arguments, _Result[list[ShikiruTask]])).result

    async def get_task(self, task_id: str) -> TaskDetail | None:
        return (
            await self._call("get_task", {"task_id": task_id}, _Result[TaskDetail | None])
        ).result

    async def resolve_escalation(
        self, escalation_id: str, kind: str, text: str | None = None
    ) -> Ack:
        resolution = {"kind": kind, "text": text}
        arguments = {"escalation_id": escalation_id, "resolution": resolution}
        return _accepted(await self._call("resolve_escalation", arguments, Ack))

    async def submit_task(self, request: str) -> Ack:
        return _accepted(await self._call("submit_task", {"request": request}, Ack))

    async def start_engine(self) -> None:
        result = await self._runner.run(self._start_argv, timeout_s=self._start_timeout_s)
        if result.returncode != 0:
            raise ShikiruRejectedError(result.stderr.strip() or result.stdout.strip())

    async def _call[M: BaseModel](
        self,
        tool: str,
        arguments: dict[str, Any],
        reply_type: type[M],
        read_timeout_s: float | None = None,
    ) -> M:
        session = self._session
        if session is None:
            message = "the link to Shikiru is down"
            raise ShikiruLinkDownError(message)
        timeout_s = self._call_timeout_s if read_timeout_s is None else read_timeout_s
        try:
            return await _invoke(session, tool, arguments, reply_type, timeout_s)
        except ShikiruLinkDownError:
            self._drop(session)
            raise

    def _drop(self, session: ToolSession) -> None:
        if self._session is session:
            self._session = None
            self._dropped.set()


async def _invoke[M: BaseModel](
    session: ToolSession,
    tool: str,
    arguments: dict[str, Any],
    reply_type: type[M],
    timeout_s: float,
) -> M:
    try:
        # keep: the SDK raises these for a result that breaks the tool's schema, not a lost link
        result = await session.call_tool(tool, arguments, timedelta(seconds=timeout_s))
    except (RuntimeError, ValidationError) as error:
        message = f"{tool} returned an unreadable result: {error}"
        raise ShikiruProtocolError(message) from error
    except Exception as error:
        raise ShikiruLinkDownError(str(error)) from error
    if result.isError:
        raise ShikiruRejectedError(_error_text(result))
    try:
        return reply_type.model_validate(result.structuredContent)
    except ValidationError as error:
        message = f"{tool} returned an unreadable result: {error}"
        raise ShikiruProtocolError(message) from error


def _accepted(ack: Ack) -> Ack:
    if ack.status == AckStatus.REJECTED:
        raise ShikiruRejectedError(ack.message or "Shikiru rejected the command")
    return ack


async def probe_link(connect: Connector, timeout_s: float) -> None:
    """Bring the link up once and ask `status`, for `doctor`; raises a ShikiruError if it fails."""
    try:
        async with asyncio.timeout(timeout_s), connect() as session:
            await _invoke(session, "status", {}, EngineStatus, timeout_s)
    except ShikiruError:
        raise
    except Exception as error:
        message = f"the link did not come up: {error}"
        raise ShikiruLinkDownError(message) from error


def _error_text(result: CallToolResult) -> str:
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    return " ".join(texts)
