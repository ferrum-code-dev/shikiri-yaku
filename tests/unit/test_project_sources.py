from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from mcp.types import CallToolResult, ListToolsResult

from shikiri_yaku.bootstrap import project_jobs, shikiru_client
from shikiri_yaku.journal import CursorStore, Database, SignalStore
from shikiri_yaku.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from shikiri_yaku.adapters.shikiru_mcp import ToolSession

START = datetime(2026, 10, 28, 9, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.current = START

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)
        resumed = asyncio.Event()
        asyncio.get_running_loop().call_soon(resumed.set)
        await resumed.wait()


class _Session:
    def __init__(self, clock: _Clock, database: Database, *, clean_stop: bool) -> None:
        self.clock = clock
        self.database = database
        self.clean_stop = clean_stop
        self.stop_observed = False
        self.status_times: list[datetime] = []
        self.successful_status_calls = 0
        self.finished = asyncio.Event()
        self.active = False
        self.unreachable_until = START
        self.open_health_on_recovery: list[str] = []
        self.failed = False
        self.poison_until = START
        self.open_health_on_good_batch: list[str] | None = None
        self.status_unreachable_until = START
        self.open_health_on_status_recovery: list[str] | None = None

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[ToolSession]:
        self.active = True
        try:
            yield self
        finally:
            self.active = False

    async def list_tools(self) -> ListToolsResult:
        return ListToolsResult(tools=[])

    async def call_tool(
        self, name: str, arguments: dict[str, Any], read_timeout_seconds: timedelta
    ) -> CallToolResult:
        del read_timeout_seconds
        if name == "wait_for_events":
            if self.clock.now() <= self.unreachable_until and self.unreachable_until > START:
                self.failed = True
                message = "event link lost"
                raise ConnectionError(message)
            if self.failed:
                self.open_health_on_recovery = self.open_health()
                self.failed = False
            result = await self._events(arguments)
        elif name == "list_escalations":
            result = []
        else:
            assert name == "status"
            self.status_times.append(self.clock.now())
            if self.clock.now() < self.status_unreachable_until:
                message = "status link lost"
                raise ConnectionError(message)
            if (
                self.status_unreachable_until > START
                and self.open_health_on_status_recovery is None
            ):
                self.open_health_on_status_recovery = self.open_health()
            self.stop_observed = self.clock.now() >= START + timedelta(minutes=1)
            self.successful_status_calls += 1
            if self.successful_status_calls >= 5:
                self.finished.set()
            return CallToolResult(
                content=[],
                structuredContent={
                    "engine_up": self.clean_stop
                    and self.clock.now() < START + timedelta(minutes=1),
                    "quotas": {},
                },
            )
        return CallToolResult(content=[], structuredContent={"result": result})

    def open_health(self) -> list[str]:
        rows = self.database.connection.execute(
            "SELECT id FROM signals WHERE kind = 'health' AND state != 'closed'"
        )
        return [row["id"] for row in rows]

    async def _events(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        if self.clock.now() < self.poison_until:
            return [{"id": 1, "kind": "escalation", "ts": START.isoformat(), "data": {}}]
        if self.poison_until > START and self.open_health_on_good_batch is None:
            self.open_health_on_good_batch = self.open_health()
        events: list[dict[str, Any]] = [
            {
                "id": 1,
                "kind": "engine_started",
                "ts": START.isoformat(),
                "task_id": None,
                "message": "started",
                "data": {},
            }
        ]
        if self.clean_stop and self.stop_observed:
            events.append(
                {
                    "id": 2,
                    "kind": "engine_stopped",
                    "ts": (START + timedelta(minutes=1)).isoformat(),
                    "task_id": None,
                    "message": "stopped",
                    "data": {},
                }
            )
        available = [
            event
            for event in events
            if event["id"] > arguments["since_id"]
            and datetime.fromisoformat(event["ts"]) <= self.clock.now()
        ]
        if available:
            return available[:1]
        await self.clock.sleep(arguments["timeout_s"])
        return []


def _settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "product": "shop",
            "components": [{"name": "api", "path": str(tmp_path)}],
            "channels": {"telegram": {"owners": [123]}},
        }
    )


async def _run(settings: Settings, database: Database, session: _Session) -> None:
    component = settings.components[0]
    client = shikiru_client(
        settings, component, session.clock, lambda _executable, _path: session.connect
    )
    settled = asyncio.Event()
    settled.set()
    jobs = project_jobs(settings, component, client, database, session.clock, settled=settled)
    tasks = [asyncio.create_task(job()) for job in jobs]
    try:
        await asyncio.wait_for(session.finished.wait(), timeout=5)
    finally:
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert not session.active


@pytest.mark.parametrize("clean_stop", [False, True])
def test_runtime_syncs_lifecycle_before_deciding_crash_or_deliberate_stop(
    tmp_path: Path, *, clean_stop: bool
) -> None:
    database = Database.open(":memory:")
    try:
        session = _Session(_Clock(), database, clean_stop=clean_stop)
        asyncio.run(_run(_settings(tmp_path), database, session))
        cursor = CursorStore(database, SignalStore(database)).get("api")
        assert cursor is not None
        assert cursor.last_event_id == (2 if clean_stop else 1)
        assert cursor.respected_stop_id == (2 if clean_stop else None)
        ids = [row["id"] for row in database.connection.execute("SELECT id FROM signals")]
        assert ids == ([] if clean_stop else ["api:engine_down:1"])
        assert session.status_times[:4] == [START + timedelta(minutes=i) for i in range(4)]
    finally:
        database.close()


def test_runtime_event_link_outage_opens_health_and_recovers_before_checking_liveness(
    tmp_path: Path,
) -> None:
    database = Database.open(":memory:")
    try:
        session = _Session(_Clock(), database, clean_stop=False)
        session.unreachable_until = START + timedelta(minutes=11)
        asyncio.run(_run(_settings(tmp_path), database, session))
        rows = database.connection.execute("SELECT id, state FROM signals WHERE kind = 'health'")
        assert [tuple(row) for row in rows] == [("api:health:link:2026-10-28T09:00Z", "closed")]
        assert session.open_health_on_recovery == ["api:health:link:2026-10-28T09:00Z"]
        assert session.status_times[0] > session.unreachable_until
    finally:
        database.close()


def test_runtime_malformed_event_opens_one_health_signal_and_blocks_liveness_until_good(
    tmp_path: Path,
) -> None:
    database = Database.open(":memory:")
    try:
        session = _Session(_Clock(), database, clean_stop=False)
        session.poison_until = START + timedelta(minutes=3)
        asyncio.run(_run(_settings(tmp_path), database, session))
        rows = database.connection.execute("SELECT id, state FROM signals WHERE kind = 'health'")
        assert [tuple(row) for row in rows] == [
            ("api:health:events:2026-10-28T09:00:00.000000Z", "closed")
        ]
        assert session.open_health_on_good_batch == [
            "api:health:events:2026-10-28T09:00:00.000000Z"
        ]
        assert min(session.status_times) >= session.poison_until
    finally:
        database.close()


def test_runtime_status_link_outage_survives_good_event_polls_and_recovers(
    tmp_path: Path,
) -> None:
    database = Database.open(":memory:")
    try:
        session = _Session(_Clock(), database, clean_stop=False)
        session.status_unreachable_until = START + timedelta(minutes=12)
        asyncio.run(_run(_settings(tmp_path), database, session))
        rows = database.connection.execute("SELECT id, state FROM signals WHERE kind = 'health'")
        assert [tuple(row) for row in rows] == [("api:health:link:2026-10-28T09:00Z", "closed")]
        assert session.open_health_on_status_recovery == ["api:health:link:2026-10-28T09:00Z"]
        assert len(session.status_times) >= 13
        assert session.status_times[-1] >= session.status_unreachable_until
        cursor = CursorStore(database, SignalStore(database)).get("api")
        assert cursor is not None
        assert cursor.last_event_id == 1
    finally:
        database.close()
