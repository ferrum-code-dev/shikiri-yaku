"""The /status report: each component's engine, its tasks and its quota blocks (design §7)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from shikiri_yaku.ports import ShikiruError, TaskStatus

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from shikiri_yaku.ports import LifecycleJournal, RungState, RungStates, ShikiruClient

# keep: Shikiru's used_pct runs 0 to 100; design §6.2 counts 100 or more as exhausted
EXHAUSTED_PCT: Final = 100.0
_REPORTED_TASKS: Final = (TaskStatus.RUNNING, TaskStatus.QUEUED, TaskStatus.ESCALATED)


class Engine(StrEnum):
    UP = "up"
    DOWN = "down"
    NEVER_STARTED = "never started"


@dataclass(frozen=True, slots=True)
class ComponentStatus:
    """One component's state; `engine` is None when Shikiru did not answer."""

    project: str
    engine: Engine | None
    running: int = 0
    queued: int = 0
    escalated: int = 0
    blocks: int = 0


class Status:
    """Reads each client's engine, tasks and quotas; the journal tells never-started apart."""

    def __init__(
        self,
        clients: Mapping[str, ShikiruClient],
        lifecycles: LifecycleJournal,
        rungs: RungStates | None = None,
    ) -> None:
        self._clients = clients
        self._lifecycles = lifecycles
        self._rungs = rungs

    async def components(self) -> list[ComponentStatus]:
        return [await self._component(project, client) for project, client in self._clients.items()]

    async def report(self) -> str:
        sections = [_render(component) for component in await self.components()]
        if self._rungs is not None:
            sections.append(_render_rungs(self._rungs.rung_states()))
        return "\n\n".join(sections)

    async def _component(self, project: str, client: ShikiruClient) -> ComponentStatus:
        try:
            status = await client.status()
            tasks = await client.list_tasks(_REPORTED_TASKS)
        except ShikiruError:
            return ComponentStatus(project, None)
        counts = Counter(task.status for task in tasks)
        return ComponentStatus(
            project,
            self._engine(project, up=status.engine_up),
            running=counts[TaskStatus.RUNNING],
            queued=counts[TaskStatus.QUEUED],
            escalated=counts[TaskStatus.ESCALATED],
            blocks=sum(
                window.used_pct >= EXHAUSTED_PCT
                for windows in status.quotas.values()
                for window in windows
            ),
        )

    def _engine(self, project: str, *, up: bool) -> Engine:
        if up:
            return Engine.UP
        state = self._lifecycles.state(project)
        if state is None or state.lifecycle is None:
            return Engine.NEVER_STARTED
        return Engine.DOWN


def _render_rungs(states: Sequence[RungState]) -> str:
    return "\n".join(["Reasoner", *(_rung_line(state) for state in states)])


def _rung_line(state: RungState) -> str:
    if state.cooling_until is not None:
        return f"{state.name}: cooling until {state.cooling_until.isoformat(timespec='minutes')}"
    if state.last_failure is not None:
        return f"{state.name}: ready, last failure: {state.last_failure}"
    return f"{state.name}: ready"


def _render(component: ComponentStatus) -> str:
    if component.engine is None:
        return f"{component.project}\nShikiru did not answer."
    return "\n".join(
        [
            component.project,
            f"Engine: {component.engine}",
            (
                f"Tasks: {component.running} running, {component.queued} queued, "
                f"{component.escalated} escalated"
            ),
            f"Quota blocks: {component.blocks}",
        ]
    )
