from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest

from shikiri_yaku.app.status import Engine, Status
from shikiri_yaku.domain.signal import Lifecycle
from shikiri_yaku.journal import CursorStore, Database, JournalSources, SignalStore
from shikiri_yaku.ports import (
    EngineStatus,
    EventKinds,
    QuotaWindow,
    RungState,
    ShikiruLinkDownError,
    ShikiruTask,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from shikiri_yaku.app.status import ComponentStatus
    from shikiri_yaku.ports import ShikiruClient

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
PROJECT = "api"
STARTED = Lifecycle(event_id=10, kind=EventKinds.ENGINE_STARTED, has_reason=False, at=NOW)


class _Client:
    """A Shikiru client with fixed answers; `down` makes every call fail as a dropped link."""

    def __init__(
        self,
        *,
        engine_up: bool = True,
        quotas: dict[str, list[QuotaWindow]] | None = None,
        tasks: list[ShikiruTask] | None = None,
        down: bool = False,
    ) -> None:
        self.engine_up = engine_up
        self.quotas = {} if quotas is None else quotas
        self.tasks = [] if tasks is None else tasks
        self.down = down

    async def status(self) -> EngineStatus:
        if self.down:
            raise ShikiruLinkDownError
        return EngineStatus(engine_up=self.engine_up, quotas=self.quotas)

    async def list_tasks(self, statuses: Sequence[str] | None = None) -> list[ShikiruTask]:
        if self.down:
            raise ShikiruLinkDownError
        return [task for task in self.tasks if statuses is None or task.status in statuses]


@pytest.fixture
def database() -> Generator[Database]:
    journal = Database.open(":memory:")
    yield journal
    journal.close()


def _task(status: str) -> ShikiruTask:
    return ShikiruTask(id="T-1", parent_id=None, status=status, title="work")


def _components(database: Database, clients: dict[str, _Client]) -> list[ComponentStatus]:
    signals = SignalStore(database)
    sources = JournalSources(CursorStore(database, signals), signals)
    typed = {name: cast("ShikiruClient", client) for name, client in clients.items()}
    return asyncio.run(Status(typed, sources).components())


@pytest.mark.parametrize(
    ("engine_up", "lifecycle", "expected"),
    [
        (True, None, Engine.UP),
        (True, STARTED, Engine.UP),
        (False, None, Engine.NEVER_STARTED),
        (False, STARTED, Engine.DOWN),
    ],
)
def test_engine_is_up_down_or_never_started_by_its_status_and_journal_history(
    database: Database, *, engine_up: bool, lifecycle: Lifecycle | None, expected: Engine
) -> None:
    CursorStore(database, SignalStore(database)).record_batch(PROJECT, [], 10, lifecycle, NOW)

    (component,) = _components(database, {PROJECT: _Client(engine_up=engine_up)})

    assert component.engine == expected


def test_running_queued_and_escalated_tasks_are_counted_by_their_status(database: Database) -> None:
    tasks = [_task(status) for status in ("running", "queued", "queued", "escalated")]

    (component,) = _components(database, {PROJECT: _Client(tasks=tasks)})

    assert (component.running, component.queued, component.escalated) == (1, 2, 1)


@pytest.mark.parametrize(("used_pct", "blocks"), [(99.0, 0), (100.0, 1)])
def test_a_quota_window_at_100_percent_is_a_block(
    database: Database, used_pct: float, blocks: int
) -> None:
    quotas = {"claude": [QuotaWindow(name="five_hour", used_pct=used_pct, resets_at=None)]}

    (component,) = _components(database, {PROJECT: _Client(quotas=quotas)})

    assert component.blocks == blocks


def test_a_component_that_does_not_answer_does_not_hide_the_others(database: Database) -> None:
    components = _components(
        database,
        {"api": _Client(down=True), "web": _Client(tasks=[_task("running")])},
    )

    assert [component.engine for component in components] == [None, Engine.UP]
    assert components[1].running == 1


class _Rungs:
    """The ladder's rung states, fixed."""

    def __init__(self, states: Sequence[RungState]) -> None:
        self._states = states

    def rung_states(self) -> Sequence[RungState]:
        return self._states


def test_the_report_lists_each_rung_with_its_cooling_time_or_that_it_is_ready(
    database: Database,
) -> None:
    signals = SignalStore(database)
    sources = JournalSources(CursorStore(database, signals), signals)
    cooling = RungState("claude:haiku", last_failure="no usable answer", cooling_until=NOW)
    ready = RungState("codex:gpt-5", last_failure=None, cooling_until=None)
    clients = {"api": cast("ShikiruClient", _Client())}

    report = asyncio.run(Status(clients, sources, _Rungs([cooling, ready])).report())

    assert f"claude:haiku: cooling until {NOW.isoformat(timespec='minutes')}" in report
    assert "codex:gpt-5: ready" in report


@pytest.mark.parametrize(
    ("component", "sections"),
    [(None, ["api", "web", "Reasoner"]), ("web", ["web"])],
)
def test_a_report_for_one_component_has_only_its_section_and_no_reasoner_rungs(
    database: Database, component: str | None, sections: list[str]
) -> None:
    signals = SignalStore(database)
    sources = JournalSources(CursorStore(database, signals), signals)
    clients = {"api": cast("ShikiruClient", _Client()), "web": cast("ShikiruClient", _Client())}
    ready = RungState("codex:gpt-5", last_failure=None, cooling_until=None)

    report = asyncio.run(Status(clients, sources, _Rungs([ready])).report(component))

    assert [section.split("\n")[0] for section in report.split("\n\n")] == sections
