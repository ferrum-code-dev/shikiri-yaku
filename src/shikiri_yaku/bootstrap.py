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
from shikiri_yaku.adapters.stderr import StderrReporter
from shikiri_yaku.adapters.telegram import TelegramChannel, probe_bot
from shikiri_yaku.adapters.telegram_http import HttpBotApi
from shikiri_yaku.app.dispatcher import Dispatcher
from shikiri_yaku.app.event_source import EventSource
from shikiri_yaku.app.health_source import HealthSource
from shikiri_yaku.app.liveness_source import LivenessSource
from shikiri_yaku.app.outbox_sender import OutboxSender
from shikiri_yaku.app.project_sources import ProjectSources
from shikiri_yaku.app.router import Router
from shikiri_yaku.app.scheduler import Scheduler
from shikiri_yaku.domain.rule_classifier import RuleClassifier
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.domain.signal import Kind
from shikiri_yaku.extensions.product.extension import product_extension
from shikiri_yaku.extensions.product.forwarder import ProductForwarder
from shikiri_yaku.instance import JOURNAL_FILE_NAME
from shikiri_yaku.journal import (
    CursorStore,
    Database,
    JournalError,
    JournalEvents,
    JournalOutbox,
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
    from shikiri_yaku.adapters.telegram import BotApi
    from shikiri_yaku.cli import Job
    from shikiri_yaku.domain.classification import Category, Classifier
    from shikiri_yaku.domain.extension import Extension, Handler
    from shikiri_yaku.ports import Channel, Clock, Forwarder, Outbox, Reporter
    from shikiri_yaku.settings import Component, Settings


SHIKIRU_EXECUTABLE: Final = "shikiru"
TELEGRAM_CHANNEL: Final = "telegram"

type LinkFactory = Callable[[str, Path], Connector]
type BotFactory = Callable[[Settings], BotApi]


@dataclass(frozen=True, slots=True)
class Composition:
    settings: Settings
    clock: Clock
    journal: Database
    link_probes: Mapping[str, Callable[[], Awaitable[None]]]
    telegram_probe: Callable[[], Awaitable[None]]
    jobs: Sequence[Job]

    def close(self) -> None:
        self.journal.close()


def http_bot(settings: Settings) -> BotApi:
    """Return the Bot API transport of the instance's token file and call timeout."""
    return HttpBotApi(settings.telegram.token_file, settings.operator.telegram_call_timeout_s)


def compose(
    settings: Settings,
    directory: Path,
    *,
    link: LinkFactory = stdio_connector,
    bot: BotFactory = http_bot,
) -> Composition:
    """Build the objects of one instance; the caller closes the result.

    `link` opens a project's MCP link and `bot` makes the Telegram transport; tests give fakes.
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
    reporter = StderrReporter()
    clients = {
        component.name: shikiru_client(settings, component, clock, link)
        for component in settings.components
    }
    jobs = [
        job
        for component in settings.components
        for job in project_jobs(settings, component, clients[component.name], journal, clock)
    ]
    outbox = JournalOutbox(journal, MessageStore(journal, reporter))
    extensions = registered_extensions()
    forwarder = ProductForwarder(
        settings.product, clients, outbox, clock, _owner_destinations(settings)
    )
    jobs += routing_jobs(
        settings,
        journal,
        clock,
        classifier=RuleClassifier(extensions),
        handlers={extension.category: extension.handler for extension in extensions},
        fallback=forwarder,
        reporter=reporter,
    )
    bot_api = bot(settings)
    channel = TelegramChannel(bot_api, clock, outbox)
    jobs.append(outbox_job(settings, outbox, clock, channel))
    return Composition(settings, clock, journal, probes, partial(probe_bot, bot_api), jobs)


def registered_extensions() -> list[Extension]:
    """Return the extensions of this build, in classification order; the catch-all comes last."""
    return [product_extension()]


def shikiru_client(
    settings: Settings, component: Component, clock: Clock, link: LinkFactory
) -> ShikiruMcpClient:
    """Return the project's one client, which its supervisor, sources and forwarder share."""
    operator = settings.operator
    return ShikiruMcpClient(
        link(SHIKIRU_EXECUTABLE, component.path),
        clock,
        backoff_initial_s=operator.link_backoff_initial_s,
        backoff_max_s=operator.link_backoff_max_s,
        call_timeout_s=operator.link_call_timeout_s,
        runner=ProcessCommandRunner(),
        start_argv=[SHIKIRU_EXECUTABLE, "--project", str(component.path), "start"],
        start_timeout_s=operator.link_start_timeout_s,
    )


def project_jobs(
    settings: Settings,
    component: Component,
    client: ShikiruMcpClient,
    journal: Database,
    clock: Clock,
) -> list[Job]:
    """Return the link supervisor and synchronized sources of one project."""
    operator = settings.operator
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
    reporter: Reporter,
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
        schedule_log(journal, reporter),
        router,
        clock,
        interval_s=operator.scheduler_interval_s,
        reminder_first_h=settings.handlers.product.reminder_first_h,
        reminder_repeat_h=settings.handlers.product.reminder_repeat_h,
    )
    return [dispatcher.run, scheduler.run]


def outbox_job(settings: Settings, outbox: Outbox, clock: Clock, channel: Channel) -> Job:
    """Return the one sender of the outbox; it delivers questions, notices and reminders alike."""
    sender = OutboxSender(outbox, channel, clock, interval_s=settings.operator.outbox_interval_s)
    return sender.run


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command with this module's composition; the console entry point calls this."""
    return cli.main(argv, compose)


def schedule_log(journal: Database, reporter: Reporter) -> JournalSchedule:
    """Register notice kinds that need reminders until acknowledged (design §6.4)."""
    return JournalSchedule(
        journal,
        SignalStore(journal),
        MessageStore(journal, reporter),
        OpStore(journal),
        reminder_notice_kinds=frozenset({Kind.PR_REVIEW}),
    )


def _owner_destinations(settings: Settings) -> list[Destination]:
    """Return the private chat of each owner, where a forwarded signal goes (design §7)."""
    return [Destination(TELEGRAM_CHANNEL, str(owner)) for owner in settings.telegram.owners]
