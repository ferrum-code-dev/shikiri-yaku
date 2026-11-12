"""The ShikiruClient over one project's `shikiru --project <path> mcp` on stdio."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol, TextIO, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent
from pydantic import BaseModel, ConfigDict, ValidationError

from shikiri_yaku.ports import (
    Ack,
    AckStatus,
    DoctorCheck,
    DoctorReport,
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
from shikiri_yaku.settings import STDERR_DRAIN_S

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Sequence
    from pathlib import Path
    from typing import BinaryIO

    from mcp.types import CallToolResult, ListToolsResult

    from shikiri_yaku.ports import Clock, CommandRunner

BACKOFF_FACTOR = 2
ROUTINE_REQUEST_TRACE: Final = re.compile(
    r"(?:\[[^\]]*\]\s+)?INFO\s+Processing request of type \S+"
)
# keep: the directory `shikiru init` creates in a project; its absence means the path is not one
SHIKIRU_STATE_DIRECTORY = ".shikiru"


# keep: every tool the verbs below call; doctor fails when Shikiru stops offering one of them.
DECLARED_TOOLS: Final = frozenset(
    {
        "status",
        "wait_for_events",
        "list_escalations",
        "get_escalation",
        "list_tasks",
        "get_task",
        "resolve_escalation",
        "submit_task",
    }
)


class ToolSession(Protocol):
    """The calls of an initialized MCP session that the client uses."""

    async def call_tool(
        self, name: str, arguments: dict[str, Any], read_timeout_seconds: timedelta
    ) -> CallToolResult: ...

    async def list_tools(self) -> ListToolsResult: ...


type Connector = Callable[[], AbstractAsyncContextManager[ToolSession]]


class _Result[T](BaseModel):
    """Shikiru's server wraps a list or optional result in a `result` key."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    result: T


def stdio_connector(executable: str, project: Path) -> Connector:
    """Connect to `executable --project <project> mcp`; the executable is found on PATH.

    A path without a Shikiru state directory is refused before anything starts, so the error
    names the path rather than a failed process.
    """
    parameters = StdioServerParameters(command=executable, args=["--project", str(project), "mcp"])

    @asynccontextmanager
    async def connect() -> AsyncGenerator[ToolSession]:
        if not (project / SHIKIRU_STATE_DIRECTORY).is_dir():
            message = (
                f"{project} is not a Shikiru project (no {SHIKIRU_STATE_DIRECTORY} directory); "
                "run shikiru init there"
            )
            raise ShikiruLinkDownError(message)
        async with (
            _stderr_without_request_traces() as errlog,
            stdio_client(parameters, errlog=errlog) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session

    return connect


@asynccontextmanager
async def _stderr_without_request_traces() -> AsyncGenerator[TextIO]:
    read_fd, write_fd = os.pipe()
    pump = threading.Thread(
        target=_copy_without_request_traces,
        args=(os.fdopen(read_fd, "rb"),),
        daemon=True,
    )
    pump.start()
    errlog = os.fdopen(write_fd, "w", encoding="utf-8", buffering=1)
    try:
        yield errlog
    finally:
        errlog.close()
        await asyncio.to_thread(pump.join, STDERR_DRAIN_S)


def _copy_without_request_traces(source: BinaryIO) -> None:
    with source:
        for raw in source:
            line = raw.decode("utf-8", errors="replace")
            if ROUTINE_REQUEST_TRACE.fullmatch(line.rstrip("\r\n")) is None:
                sys.stderr.write(line)
                sys.stderr.flush()


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
        doctor_argv: Sequence[str],
    ) -> None:
        self._connect = connect
        self._clock = clock
        self._runner = runner
        self._start_argv = list(start_argv)
        self._start_timeout_s = start_timeout_s
        self._doctor_argv = list(doctor_argv)
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

    @property
    def linked(self) -> bool:
        return self._session is not None

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

    async def doctor(self) -> list[DoctorCheck]:
        # keep: doctor exits nonzero when a check fails, so its report is read whatever the code
        result = await self._runner.run(self._doctor_argv, timeout_s=self._call_timeout_s)
        try:
            report = DoctorReport.model_validate_json(result.stdout)
        except ValidationError as error:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            message = f"shikiru doctor printed no readable report: {detail}"
            raise ShikiruProtocolError(message) from error
        return [check for check in report.checks if not check.ok]

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
        raise ShikiruLinkDownError(_reason(error)) from error
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
    """Bring the link up once and check it, for `doctor`.

    It checks that Shikiru offers every declared tool and that the reads without arguments
    answer in a shape the client reads. Raises a ShikiruError if any of it fails.
    """
    try:
        async with asyncio.timeout(timeout_s), connect() as session:
            await _check_offered(session)
            await _invoke(session, "status", {}, EngineStatus, timeout_s)
            await _invoke(session, "list_escalations", {}, _Result[list[EscalationView]], timeout_s)
            await _invoke(
                session, "list_tasks", {"statuses": None}, _Result[list[ShikiruTask]], timeout_s
            )
    except ShikiruError:
        raise
    except Exception as error:
        message = f"the link did not come up: {_reason(error)}"
        raise ShikiruLinkDownError(message) from error


async def _check_offered(session: ToolSession) -> None:
    offered = {tool.name for tool in (await session.list_tools()).tools}
    absent = sorted(DECLARED_TOOLS - offered)
    if absent:
        message = f"Shikiru does not offer {', '.join(absent)}"
        raise ShikiruProtocolError(message)


# keep: a group's own text names only the group, not the cause, so its leaves' texts are used
def _reason(error: BaseException) -> str:
    if not isinstance(error, BaseExceptionGroup):
        return str(error)
    group = cast("BaseExceptionGroup[BaseException]", error)
    reasons = dict.fromkeys(_reason(leaf) for leaf in group.exceptions)
    return "; ".join(reason for reason in reasons if reason)


def _error_text(result: CallToolResult) -> str:
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    return " ".join(texts)
