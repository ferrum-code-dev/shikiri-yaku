"""The /status and /details reports: each component's engine, tasks and quotas."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from shikiri_yaku.app.open_command import approval_option, install_is_pending
from shikiri_yaku.domain.outbound import moment_text
from shikiri_yaku.ports import EscalationStatus, EventKinds, ShikiruError, TaskStatus

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from shikiri_yaku.ports import (
        Clock,
        EscalationView,
        LifecycleJournal,
        OpenLog,
        QuotaWindow,
        ShikiruClient,
        ShikiruTask,
    )

# keep: Shikiru reports quota use from 0 to 100; 100 means the quota is exhausted
EXHAUSTED_PCT: Final = 100.0
# keep: a reset further away than this is shown as a weekday and time, not as a countdown
RELATIVE_RESET_MAX: Final = timedelta(hours=24)
_REPORTED_TASKS: Final = (TaskStatus.RUNNING, TaskStatus.QUEUED, TaskStatus.ESCALATED)
_MINUTES_PER_HOUR: Final = 60
_SECONDS_PER_MINUTE: Final = 60
_OK: Final = "✅"
_ATTENTION: Final = "⚠️"
_DOWN: Final = "🔴"
_RANK: Final = {_DOWN: 0, _ATTENTION: 1, _OK: 2}
_SEPARATOR: Final = " · "
_HINT: Final = "Full view: /details"
_PENDING_HINT: Final = "→ /pending"


class Engine(StrEnum):
    UP = "up"
    DOWN = "down"
    NEVER_STARTED = "never started"


_ENGINE_TEXT: Final = {
    Engine.UP: "🟢 up",
    Engine.DOWN: "🔴 down",
    Engine.NEVER_STARTED: "🆕 never started",
}
_UNREACHABLE_TEXT: Final = "📡 cannot reach Shikiru"


def _no_quotas() -> dict[str, list[QuotaWindow]]:
    return {}


@dataclass(frozen=True, slots=True)
class ComponentStatus:
    """One component's state; `engine` is None when Shikiru did not answer."""

    project: str
    engine: Engine | None
    running: tuple[ShikiruTask, ...] = ()
    queued: tuple[ShikiruTask, ...] = ()
    escalated: tuple[ShikiruTask, ...] = ()
    waiting: int = 0
    open_escalations: bool = False
    quotas: Mapping[str, Sequence[QuotaWindow]] = field(default_factory=_no_quotas)
    reason: str | None = None

    @property
    def exhausted(self) -> list[str]:
        """The agents with a quota window at its limit."""
        return [
            agent
            for agent, windows in self.quotas.items()
            if any(window.used_pct >= EXHAUSTED_PCT for window in windows)
        ]

    @property
    def marker(self) -> str:
        if self.engine is None or self.engine is Engine.DOWN:
            return _DOWN
        if (
            self.engine is Engine.NEVER_STARTED
            or self.open_escalations
            or self.waiting
            or self.exhausted
        ):
            return _ATTENTION
        return _OK


class Status:
    """Reads each client's engine, tasks and quotas; the journal tells never-started apart."""

    def __init__(
        self,
        clients: Mapping[str, ShikiruClient],
        lifecycles: LifecycleJournal,
        clock: Clock,
        open_log: OpenLog,
        *,
        approval_expiry_h: int | None = None,
    ) -> None:
        self._clients = clients
        self._lifecycles = lifecycles
        self._clock = clock
        self._open_log = open_log
        self._approval_expiry_h = approval_expiry_h

    async def components(self, component: str | None = None) -> list[ComponentStatus]:
        """Return each component's status, or only the named component's."""
        return [
            await self._component(project, client)
            for project, client in self._clients.items()
            if component is None or project == component
        ]

    async def report(self, component: str | None = None) -> str:
        """Report one line per component, those that need attention first."""
        ordered = sorted(await self.components(component), key=lambda one: _RANK[one.marker])
        return "\n".join([*(_line(one) for one in ordered), _HINT])

    async def details(self, component: str | None = None) -> str:
        """Report every component in full: all tasks, each quota and why one is down."""
        now = self._clock.now()
        ordered = sorted(await self.components(component), key=lambda one: _RANK[one.marker])
        return "\n\n".join(_detail(one, now) for one in ordered)

    async def _component(
        self,
        project: str,
        client: ShikiruClient,
    ) -> ComponentStatus:
        try:
            status = await client.status()
            tasks = await client.list_tasks(_REPORTED_TASKS)
            views = await client.list_escalations()
        except ShikiruError as error:
            return ComponentStatus(project, None, reason=str(error) or type(error).__name__)
        engine = self._engine(project, up=status.engine_up)
        return ComponentStatus(
            project,
            engine,
            running=_with(tasks, TaskStatus.RUNNING),
            queued=_with(tasks, TaskStatus.QUEUED),
            escalated=_with(tasks, TaskStatus.ESCALATED),
            waiting=self._waiting(project, views),
            open_escalations=any(view.escalation.status == EscalationStatus.OPEN for view in views),
            quotas=status.quotas,
            reason=self._down_reason(project) if engine is Engine.DOWN else None,
        )

    def _waiting(self, project: str, views: Sequence[EscalationView]) -> int:
        listed = {
            view.escalation.id: view
            for view in views
            if view.escalation.status == EscalationStatus.OPEN
        }
        items = {
            signal.escalation_id
            for _, signal in self._open_log.forwarded()
            if signal.project == project and signal.escalation_id in listed
        }
        items.update(
            approval.escalation_id
            for approval in self._open_log.pending_approvals()
            if approval.project == project
            and approval.escalation_id in listed
            and approval_option(listed[approval.escalation_id], approval.kind) is not None
        )
        expiry_h = self._approval_expiry_h
        if expiry_h is not None:
            now = self._clock.now()
            items.update(
                op.payload.escalation_id
                for op in self._open_log.pending_install_approvals()
                if op.project == project
                and op.payload.escalation_id in listed
                and install_is_pending(op, now, expiry_h)
            )
        return len(items)

    def _engine(self, project: str, *, up: bool) -> Engine:
        if up:
            return Engine.UP
        state = self._lifecycles.state(project)
        if state is None or state.lifecycle is None:
            return Engine.NEVER_STARTED
        return Engine.DOWN

    def _down_reason(self, project: str) -> str | None:
        state = self._lifecycles.state(project)
        if state is None or state.lifecycle is None:
            return None
        lifecycle = state.lifecycle
        at = moment_text(lifecycle.at, self._clock.now())
        # keep: the journal stores only whether a stop had a reason, not its text
        if lifecycle.kind == EventKinds.ENGINE_STOPPED:
            return f"stopped at {at}, reason not recorded here"
        return f"no stop recorded since the start at {at}, reason unknown"


def _with(tasks: Sequence[ShikiruTask], status: str) -> tuple[ShikiruTask, ...]:
    return tuple(task for task in tasks if task.status == status)


def _line(component: ComponentStatus) -> str:
    if component.engine is None:
        parts = [_UNREACHABLE_TEXT]
    else:
        parts = [_ENGINE_TEXT[component.engine]]
        if component.running:
            parts.append(f"🏃 {len(component.running)} running")
        if component.waiting:
            parts.append(f"🙋 {component.waiting} waiting {_PENDING_HINT}")
        if component.exhausted:
            parts.append(f"⏳ quota ({', '.join(component.exhausted)})")
        if len(parts) == 1 and component.marker == _OK:
            parts.append("💤 idle")
    return f"{component.marker} {component.project}: {_SEPARATOR.join(parts)}"


def _detail(component: ComponentStatus, now: datetime) -> str:
    if component.engine is None:
        engine = f"{_UNREACHABLE_TEXT} ({component.reason})"
        return f"{component.marker} {component.project}\nEngine: {engine}"
    engine = _ENGINE_TEXT[component.engine]
    if component.reason:
        engine = f"{engine} ({component.reason})"
    lines = [f"{component.marker} {component.project}", f"Engine: {engine}"]
    for title, tasks in (
        ("🏃 Running", component.running),
        ("🕓 Queued", component.queued),
        ("🙋 Escalated", component.escalated),
    ):
        lines.append(f"{title}: {len(tasks)}" if tasks else f"{title}: none")
        lines.extend(f"- {task.id} {task.title}" for task in tasks)
    if component.waiting:
        lines.append(f"🙋 Waiting for you: {component.waiting} {_PENDING_HINT}")
    lines.append("⏳ Quota:" if component.quotas else "⏳ Quota: none reported")
    for agent, windows in component.quotas.items():
        lines.extend(f"- {agent} {_window(window, now)}" for window in windows)
    return "\n".join(lines)


def _window(window: QuotaWindow, now: datetime) -> str:
    text = f"{window.name} {window.used_pct:.0f}% used"
    if window.resets_at is None:
        return text
    return f"{text}, {_reset(window.resets_at, now)}"


def _reset(resets_at: datetime, now: datetime) -> str:
    left = resets_at - now
    if left <= timedelta(0):
        return "reset is due"
    if left > RELATIVE_RESET_MAX:
        return f"resets {resets_at.astimezone(UTC).strftime('%a %H:%M UTC')}"
    minutes = max(1, round(left.total_seconds() / _SECONDS_PER_MINUTE))
    hours, minutes = divmod(minutes, _MINUTES_PER_HOUR)
    if hours == 0:
        return f"resets in {minutes} min"
    return f"resets in {hours} h {minutes} min" if minutes else f"resets in {hours} h"
