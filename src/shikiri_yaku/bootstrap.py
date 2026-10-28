"""The composition root and the console entry point: the only module that builds concrete objects.

Only what exists is wired. A port whose adapter is not written yet is not in the composition; its
adapter is added here when it is written. `main` gives the command line this module's composition,
so the command line never imports this module and the concrete objects stay here (design §12).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Final

from shikiri_yaku import cli
from shikiri_yaku.adapters.clock import SystemClock
from shikiri_yaku.adapters.commands import ProcessCommandRunner
from shikiri_yaku.adapters.shikiru_mcp import ShikiruMcpClient, probe_link, stdio_connector
from shikiri_yaku.app.dispatcher import Dispatcher
from shikiri_yaku.app.event_source import EventSource
from shikiri_yaku.app.health_source import HealthSource
from shikiri_yaku.app.liveness_source import LivenessSource
from shikiri_yaku.app.project_sources import ProjectSources
from shikiri_yaku.app.router import Router
from shikiri_yaku.app.scheduler import Scheduler
from shikiri_yaku.domain.signal import Kind
from shikiri_yaku.instance import JOURNAL_FILE_NAME
from shikiri_yaku.journal import (
    CursorStore,
    Database,
    JournalError,
    JournalEvents,
    JournalRouting,
    JournalSchedule,
    JournalSources,
    MessageStore,
    OpStore,
    SignalStore,
)
from shikiri_yaku.ports import CompositionError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from shikiri_yaku.adapters.shikiru_mcp import Connector
    from shikiri_yaku.cli import Job
    from shikiri_yaku.domain.classification import Category, Classifier
    from shikiri_yaku.domain.extension import Handler
    from shikiri_yaku.ports import Channel, Clock, Forwarder
    from shikiri_yaku.settings import Component, Settings


SHIKIRU_EXECUTABLE: Final = "shikiru"

type LinkFactory = Callable[[str, Path], Connector]


@dataclass(frozen=True, slots=True)
class Composition:
    settings: Settings
    clock: Clock
    journal: Database
    link_probes: Mapping[str, Callable[[], Awaitable[None]]]
    jobs: Sequence[Job]

    def close(self) -> None:
        self.journal.close()


def compose(
    settings: Settings, directory: Path, *, link: LinkFactory = stdio_connector
) -> Composition:
    """Build the objects of one instance; the caller closes the result.

    `link` opens a project's MCP link; tests give it a fake.
    """
    try:
        journal = Database.open(directory / JOURNAL_FILE_NAME)
    except JournalError as error:
        raise CompositionError(str(error)) from error
    timeout_s = settings.operator.link_call_timeout_s
    probes = {
        component.name: partial(probe_link, link(SHIKIRU_EXECUTABLE, component.path), timeout_s)
        for component in settings.components
    }
    clock = SystemClock()
    jobs = [
        job
        for component in settings.components
        for job in project_jobs(settings, component, journal, clock, link)
    ]
    return Composition(settings, clock, journal, probes, jobs)


def project_jobs(
    settings: Settings, component: Component, journal: Database, clock: Clock, link: LinkFactory
) -> list[Job]:
    """Return the link supervisor and synchronized sources of one project."""
    operator = settings.operator
    client = ShikiruMcpClient(
        link(SHIKIRU_EXECUTABLE, component.path),
        clock,
        backoff_initial_s=operator.link_backoff_initial_s,
        backoff_max_s=operator.link_backoff_max_s,
        call_timeout_s=operator.link_call_timeout_s,
        runner=ProcessCommandRunner(),
        start_argv=[SHIKIRU_EXECUTABLE, "--project", str(component.path), "start"],
        start_timeout_s=operator.link_start_timeout_s,
    )
    signals = SignalStore(journal)
    cursors = CursorStore(journal, signals)
    sources = JournalSources(cursors, signals)
    health = HealthSource(component.name, sources, clock, operator.link_unreachable_min)
    events = EventSource(
        component.name, client, JournalEvents(cursors), clock, operator.event_wait_s
    )
    liveness = LivenessSource(
        component.name,
        client,
        sources,
        clock,
        stop_grace_min=operator.stop_grace_min,
        events=events,
    )
    project_sources = ProjectSources(
        events,
        liveness,
        health,
        clock,
        event_wait_s=operator.event_wait_s,
        interval_s=operator.liveness_interval_s,
    )
    return [client.supervise, project_sources.run]


def routing_jobs(
    settings: Settings,
    journal: Database,
    clock: Clock,
    *,
    classifier: Classifier,
    handlers: Mapping[Category, Handler],
    fallback: Forwarder,
    channel: Channel,
) -> list[Job]:
    """Return the Dispatcher and the Scheduler, which share one Router."""
    operator = settings.operator
    signals = SignalStore(journal)
    routing = JournalRouting(journal, signals)
    router = Router(
        classifier,
        handlers,
        fallback,
        routing,
        clock,
        handler_timeout_s=operator.handler_timeout_s,
        forward_retry_s=operator.forward_retry_s,
    )
    dispatcher = Dispatcher(routing, router, clock, interval_s=operator.dispatch_interval_s)
    scheduler = Scheduler(
        schedule_log(journal),
        router,
        channel,
        clock,
        interval_s=operator.scheduler_interval_s,
        reminder_first_h=settings.handlers.product.reminder_first_h,
        reminder_repeat_h=settings.handlers.product.reminder_repeat_h,
    )
    return [dispatcher.run, scheduler.run]


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command with this module's composition; the console entry point calls this."""
    return cli.main(argv, compose)


def schedule_log(journal: Database) -> JournalSchedule:
    """Register notice kinds that need reminders until acknowledged (design §6.4)."""
    return JournalSchedule(
        journal,
        SignalStore(journal),
        MessageStore(journal),
        OpStore(journal),
        reminder_notice_kinds=frozenset({Kind.PR_REVIEW}),
    )
