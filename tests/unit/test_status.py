from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from shikiri_yaku.app.status import Engine, Status
from shikiri_yaku.domain.signal import Lifecycle
from shikiri_yaku.journal import CursorStore, Database, JournalSources, SignalStore
from shikiri_yaku.journal.open import JournalOpen
from shikiri_yaku.ports import (
    EngineStatus,
    EscalationRecord,
    EscalationResolution,
    EscalationView,
    EventKinds,
    QuotaWindow,
    ShikiruLinkDownError,
    ShikiruTask,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence

    from shikiri_yaku.app.status import ComponentStatus
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import InstallOp, OpenLog, PendingApproval, ShikiruClient

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
        escalations: list[EscalationView] | None = None,
        down: bool = False,
    ) -> None:
        self.engine_up = engine_up
        self.quotas = {} if quotas is None else quotas
        self.tasks = [] if tasks is None else tasks
        self.escalations = [] if escalations is None else escalations
        self.down = down

    async def status(self) -> EngineStatus:
        if self.down:
            raise ShikiruLinkDownError
        return EngineStatus(engine_up=self.engine_up, quotas=self.quotas)

    async def list_tasks(self, statuses: Sequence[str] | None = None) -> list[ShikiruTask]:
        if self.down:
            raise ShikiruLinkDownError
        return [task for task in self.tasks if statuses is None or task.status in statuses]

    async def list_escalations(self) -> list[EscalationView]:
        if self.down:
            raise ShikiruLinkDownError
        return self.escalations


def _view(
    escalation_id: str, task_id: str, status: str = "open", *, accept: bool = False
) -> EscalationView:
    record = EscalationRecord(
        id=escalation_id,
        task_id=task_id,
        reason="decision_required",
        headline="h",
        status=status,
        resolution=None,
    )
    return EscalationView(
        escalation=record,
        packet=None,
        question=None,
        agent_options=[],
        resolutions=[EscalationResolution(kind="accept", consequences="", recommended=False)]
        if accept
        else [],
    )


class _Waiting:
    def __init__(
        self,
        forwarded: Sequence[tuple[str, str]] = (),
        approvals: Sequence[tuple[str, str]] = (),
        installs: Sequence[tuple[str, str, str, datetime]] = (),
    ) -> None:
        self._forwarded = forwarded
        self._approvals = approvals
        self._installs = installs

    def forwarded(self) -> list[tuple[int, Signal]]:
        return [
            (1, cast("Signal", SimpleNamespace(project=project, escalation_id=escalation)))
            for project, escalation in self._forwarded
        ]

    def pending_approvals(self) -> list[PendingApproval]:
        return [
            cast(
                "PendingApproval",
                SimpleNamespace(project=project, escalation_id=escalation, kind="accept"),
            )
            for project, escalation in self._approvals
        ]

    def pending_install_approvals(self) -> list[InstallOp]:
        return [
            cast(
                "InstallOp",
                SimpleNamespace(
                    project=project,
                    state=state,
                    created_at=created,
                    payload=SimpleNamespace(escalation_id=escalation),
                ),
            )
            for project, escalation, state, created in self._installs
        ]


@pytest.fixture
def database() -> Generator[Database]:
    journal = Database.open(":memory:")
    yield journal
    journal.close()


class _Clock:
    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        raise NotImplementedError


def _task(status: str, number: int = 1) -> ShikiruTask:
    return ShikiruTask(id=f"T-{number}", parent_id=None, status=status, title=f"work {number}")


def _status(
    database: Database, clients: dict[str, _Client], open_log: OpenLog | None = None
) -> Status:
    signals = SignalStore(database)
    sources = JournalSources(CursorStore(database, signals), signals)
    typed = {name: cast("ShikiruClient", client) for name, client in clients.items()}
    return Status(
        typed, sources, _Clock(), open_log or JournalOpen(database, signals), approval_expiry_h=24
    )


def _components(database: Database, clients: dict[str, _Client]) -> list[ComponentStatus]:
    return asyncio.run(_status(database, clients).components())


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

    assert (len(component.running), len(component.queued), len(component.escalated)) == (1, 2, 1)


@pytest.mark.parametrize(("used_pct", "exhausted"), [(99.0, []), (100.0, ["claude"])])
def test_a_quota_window_at_100_percent_exhausts_its_agent(
    database: Database, used_pct: float, exhausted: list[str]
) -> None:
    quotas = {"claude": [QuotaWindow(name="five_hour", used_pct=used_pct, resets_at=None)]}

    (component,) = _components(database, {PROJECT: _Client(quotas=quotas)})

    assert component.exhausted == exhausted


def test_a_component_that_does_not_answer_does_not_hide_the_others(database: Database) -> None:
    components = _components(
        database,
        {"api": _Client(down=True), "web": _Client(tasks=[_task("running")])},
    )

    assert [component.engine for component in components] == [None, Engine.UP]
    assert len(components[1].running) == 1


def _report(database: Database, clients: dict[str, _Client], component: str | None = None) -> str:
    return asyncio.run(_status(database, clients).report(component))


def _details(database: Database, clients: dict[str, _Client], component: str | None = None) -> str:
    return asyncio.run(_status(database, clients).details(component))


def test_the_short_view_has_a_line_per_component_with_those_needing_attention_first(
    database: Database,
) -> None:
    full = [QuotaWindow(name="w", used_pct=100.0, resets_at=None)]
    exhausted = {"codex": full, "claude": full}
    clients = {
        "healthy": _Client(),
        "busy": _Client(tasks=[_task("running"), _task("running", 2)]),
        "asking": _Client(
            tasks=[_task("escalated")], quotas=exhausted, escalations=[_view("E-1", "T-1")]
        ),
        "down": _Client(engine_up=False),
        "fresh": _Client(engine_up=False),
        "lost": _Client(down=True),
    }
    CursorStore(database, SignalStore(database)).record_batch("down", [], 10, STARTED, NOW)

    lines = _report(database, clients).split("\n")

    assert lines == [
        "🔴 down: 🔴 down",
        "🔴 lost: 📡 cannot reach Shikiru",
        "⚠️ asking: 🟢 up · ⏳ quota (codex, claude)",
        "⚠️ fresh: 🆕 never started",
        "✅ healthy: 🟢 up · 💤 idle",
        "✅ busy: 🟢 up · 🏃 2 running",
        "Full view: /details",
    ]


@pytest.mark.parametrize(
    ("escalations", "log", "waiting", "attention"),
    [
        ([], _Waiting(), 0, False),
        ([_view("E-1", "T-1")], _Waiting(), 0, True),
        ([_view("E-1", "T-1", "resolved")], _Waiting(forwarded=[(PROJECT, "E-1")]), 0, False),
        (
            [_view("E-1", "T-1"), _view("E-2", "T-1")],
            _Waiting(forwarded=[(PROJECT, "E-1"), (PROJECT, "E-2")]),
            2,
            True,
        ),
        (
            [_view("E-1", "T-1", accept=True)],
            _Waiting(
                forwarded=[(PROJECT, "E-1")],
                approvals=[(PROJECT, "E-1")],
                installs=[(PROJECT, "E-1", "intended", NOW)],
            ),
            1,
            True,
        ),
        ([_view("E-1", "T-1", accept=True)], _Waiting(approvals=[(PROJECT, "E-1")]), 1, True),
        ([_view("E-1", "T-1")], _Waiting(approvals=[(PROJECT, "E-1")]), 0, True),
        ([], _Waiting(approvals=[(PROJECT, "E-1")]), 0, False),
        ([_view("E-1", "T-1")], _Waiting(forwarded=[("web", "E-1")]), 0, True),
        ([_view("E-1", "T-1")], _Waiting(installs=[(PROJECT, "E-1", "intended", NOW)]), 1, True),
        (
            [_view("E-1", "T-1")],
            _Waiting(installs=[(PROJECT, "E-1", "intended", NOW - timedelta(hours=24))]),
            0,
            True,
        ),
        ([_view("E-1", "T-1")], _Waiting(installs=[(PROJECT, "E-1", "issued", NOW)]), 0, True),
        ([], _Waiting(installs=[(PROJECT, "E-1", "intended", NOW)]), 0, False),
    ],
)
def test_the_waiting_count_has_each_actionable_item_once(
    database: Database,
    escalations: list[EscalationView],
    log: _Waiting,
    waiting: int,
    *,
    attention: bool,
) -> None:
    client = _Client(escalations=escalations)

    (component,) = asyncio.run(_status(database, {PROJECT: client}, log).components())

    assert (component.waiting, component.marker == "⚠️") == (waiting, attention)
    report = asyncio.run(_status(database, {PROJECT: client}, log).report())
    assert ("→ /pending" in report) == (waiting > 0)


def test_tasks_marked_escalated_do_not_count_without_an_open_escalation(
    database: Database,
) -> None:
    (component,) = _components(database, {PROJECT: _Client(tasks=[_task("escalated")])})

    assert (component.waiting, component.marker) == (0, "✅")


def test_the_details_list_every_task_without_a_cap_and_each_quota_with_its_reset(
    database: Database,
) -> None:
    tasks = [_task("running", n) for n in range(1, 8)] + [_task("queued", 8), _task("escalated", 9)]
    quotas = {
        "claude": [
            QuotaWindow(name="five_hour", used_pct=42.4, resets_at=NOW + timedelta(minutes=40)),
            QuotaWindow(name="week", used_pct=100.0, resets_at=NOW + timedelta(days=3)),
        ],
        "codex": [QuotaWindow(name="day", used_pct=5.0, resets_at=None)],
    }

    report = _details(database, {PROJECT: _Client(tasks=tasks, quotas=quotas)})

    assert all(f"- T-{n} work {n}" in report for n in range(1, 10))
    assert "- claude five_hour 42% used, resets in 40 min" in report
    assert "- claude week 100% used, resets Sat 09:00 UTC" in report
    assert "- codex day 5% used" in report
    assert "Reasoner" not in report
    assert "Expert" not in report


STOPPED = Lifecycle(event_id=11, kind=EventKinds.ENGINE_STOPPED, has_reason=False, at=NOW)


@pytest.mark.parametrize(
    ("lifecycle", "expected"),
    [
        (None, "Engine: 🆕 never started"),
        (STOPPED, "Engine: 🔴 down (stopped at 09:00 UTC, reason not recorded here)"),
        (
            STARTED,
            "Engine: 🔴 down (no stop recorded since the start at 09:00 UTC, reason unknown)",
        ),
    ],
)
def test_the_details_say_when_a_component_stopped_and_that_the_reason_is_not_known(
    database: Database, lifecycle: Lifecycle | None, expected: str
) -> None:
    CursorStore(database, SignalStore(database)).record_batch(PROJECT, [], 11, lifecycle, NOW)

    assert expected in _details(database, {PROJECT: _Client(engine_up=False)})


def test_the_details_name_the_reason_a_component_is_unreachable(database: Database) -> None:
    report = _details(database, {PROJECT: _Client(down=True)})

    assert "🔴 api" in report
    assert "Engine: 📡 cannot reach Shikiru (ShikiruLinkDownError)" in report


@pytest.mark.parametrize("view", [_report, _details])
def test_a_view_for_one_component_has_only_that_component(
    database: Database, view: Callable[..., str]
) -> None:
    text = view(database, {"api": _Client(), "web": _Client()}, "web")

    assert "web" in text
    assert "api" not in text
