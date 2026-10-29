"""The composition root and the console entry point: the only module that builds concrete objects.

Only what exists is wired. A port whose adapter is not written yet is not in the composition; its
adapter is added here when it is written. `main` gives the command line this module's composition,
so the command line never imports this module and the concrete objects stay here (design §12).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Final

from shikiri_yaku import cli
from shikiri_yaku.adapters.clock import SystemClock
from shikiri_yaku.adapters.commands import ProcessCommandRunner
from shikiri_yaku.adapters.shikiru_mcp import ShikiruMcpClient, probe_link, stdio_connector
from shikiri_yaku.adapters.stderr import StderrReporter
from shikiri_yaku.adapters.telegram import (
    TELEGRAM_CHANNEL,
    TelegramChannel,
    TelegramForumTopics,
    probe_bot,
)
from shikiri_yaku.adapters.telegram_http import HttpBotApi
from shikiri_yaku.adapters.telegram_topics import TelegramDestinations, TopicReconciler
from shikiri_yaku.adapters.telegram_updates import TelegramUpdates
from shikiri_yaku.app.commands import Commands
from shikiri_yaku.app.copy_editor import CopyEditor
from shikiri_yaku.app.dispatcher import Dispatcher
from shikiri_yaku.app.event_source import EventSource
from shikiri_yaku.app.goal import GoalCommand
from shikiri_yaku.app.health_source import HealthSource
from shikiri_yaku.app.inbound_poller import InboundPoller
from shikiri_yaku.app.inbound_service import OPERATOR_SCOPE, InboundService
from shikiri_yaku.app.liveness_source import LivenessSource
from shikiri_yaku.app.loop_guard import LoopGuard
from shikiri_yaku.app.notifier import Notifier
from shikiri_yaku.app.open_command import OpenCommand
from shikiri_yaku.app.outbox_sender import OutboxSender
from shikiri_yaku.app.project_sources import ProjectSources
from shikiri_yaku.app.reconciler import ResolveReconciler
from shikiri_yaku.app.replies import Replies
from shikiri_yaku.app.router import Router
from shikiri_yaku.app.scheduler import Scheduler
from shikiri_yaku.app.secret_cleanup import SecretCleanup
from shikiri_yaku.app.stats import Stats
from shikiri_yaku.app.status import Status
from shikiri_yaku.app.submit import Submitter
from shikiri_yaku.domain.rule_classifier import RuleClassifier
from shikiri_yaku.domain.signal import Kind
from shikiri_yaku.extensions.infrastructure.extension import infrastructure_extension
from shikiri_yaku.extensions.product.extension import product_extension
from shikiri_yaku.extensions.product.forwarder import ProductForwarder
from shikiri_yaku.instance import JOURNAL_FILE_NAME
from shikiri_yaku.journal import (
    CursorStore,
    Database,
    JournalActions,
    JournalError,
    JournalEvents,
    JournalFinalEdits,
    JournalInbound,
    JournalOutbox,
    JournalRetries,
    JournalRouting,
    JournalSchedule,
    JournalSources,
    JournalWaits,
    MessageStore,
    OpStore,
    SignalStore,
)
from shikiri_yaku.journal.open import JournalOpen
from shikiri_yaku.journal.reconcile import JournalReconcile
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.journal.stats import JournalStats
from shikiri_yaku.journal.submits import JournalSubmits
from shikiri_yaku.journal.topics import TopicStore
from shikiri_yaku.ports import CompositionError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from shikiri_yaku.adapters.shikiru_mcp import Connector
    from shikiri_yaku.adapters.telegram import BotApi
    from shikiri_yaku.cli import Job
    from shikiri_yaku.domain.classification import Category, Classifier
    from shikiri_yaku.domain.extension import Extension, Handler
    from shikiri_yaku.ports import (
        Channel,
        Clock,
        Destinations,
        Forwarder,
        Guard,
        Outbox,
        Reporter,
        Reshower,
        RetryLog,
        ShikiruClient,
        WaitLog,
    )
    from shikiri_yaku.settings import Component, Settings


SHIKIRU_EXECUTABLE: Final = "shikiru"

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
    recover: Callable[[], None]

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
    bot_api = bot(settings)
    topics = TopicStore(journal)
    group_id = settings.telegram.group_id
    reconciler = (
        None
        if group_id is None
        else TopicReconciler(
            topics,
            TelegramForumTopics(bot_api, group_id),
            [component.name for component in settings.components],
        )
    )
    destinations = TelegramDestinations(
        topics, owners=settings.telegram.owners, group_id=group_id, reconciler=reconciler
    )
    guard = LoopGuard(
        JournalActions(OpStore(journal)),
        clock,
        retry_budget_per_day=settings.handlers.infrastructure.retry_budget_per_day,
        restart_budget_per_hour=settings.handlers.infrastructure.restart_budget_per_hour,
    )
    retries = JournalRetries(journal, OpStore(journal))
    waits = JournalWaits(journal, OpStore(journal))
    notifier = Notifier(outbox, destinations, clock)
    infrastructure = settings.handlers.infrastructure
    extensions = registered_extensions(
        clients,
        guard,
        retries,
        clock,
        waits=waits,
        notifier=notifier,
        quota_wait_max=timedelta(hours=infrastructure.quota_wait_max_h),
        quota_wait_margin=timedelta(minutes=infrastructure.quota_wait_margin_min),
    )
    forwarder = ProductForwarder(settings.product, clients, outbox, clock, destinations)
    jobs += routing_jobs(
        settings,
        journal,
        clock,
        classifier=RuleClassifier(extensions),
        handlers={extension.category: extension.handler for extension in extensions},
        fallback=forwarder,
        reporter=reporter,
    )
    channel = TelegramChannel(bot_api, clock, outbox)
    jobs.append(outbox_job(settings, outbox, clock, channel))
    goal = goal_command(journal, clients, outbox, clock, destinations)
    jobs.append(
        inbound_job(
            settings,
            journal,
            outbox,
            clock,
            clients=clients,
            reshower=forwarder,
            bot_api=bot_api,
            goal=goal,
            destinations=destinations,
        )
    )
    jobs.append(secret_cleanup_job(settings, journal, channel, clock))
    jobs.append(copy_edit_job(settings, journal, channel, clock))
    jobs.append(reconcile_job(settings, journal, clock, clients=clients, reshower=forwarder))

    def recover() -> None:
        goal.recover()
        asyncio.run(destinations.prepare())

    return Composition(
        settings,
        clock,
        journal,
        probes,
        partial(probe_bot, bot_api, settings.telegram.group_id, settings.telegram.owners),
        jobs,
        recover,
    )


def registered_extensions(
    clients: Mapping[str, ShikiruClient],
    guard: Guard,
    retries: RetryLog,
    clock: Clock,
    *,
    waits: WaitLog,
    notifier: Notifier,
    quota_wait_max: timedelta,
    quota_wait_margin: timedelta,
) -> list[Extension]:
    """Return the extensions of this build, in classification order; the catch-all comes last."""
    infrastructure = infrastructure_extension(
        clients,
        guard,
        retries,
        clock,
        waits=waits,
        notifier=notifier,
        quota_wait_max=quota_wait_max,
        quota_wait_margin=quota_wait_margin,
    )
    return [infrastructure, product_extension()]


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
        component.name,
        client,
        JournalEvents(journal, cursors, OpStore(journal)),
        clock,
        operator.event_wait_s,
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


def inbound_job(
    settings: Settings,
    journal: Database,
    outbox: Outbox,
    clock: Clock,
    *,
    clients: Mapping[str, ShikiruClient],
    reshower: Reshower,
    bot_api: BotApi,
    goal: GoalCommand,
    destinations: Destinations,
) -> Job:
    """Return the one poller of the PO's chat; it applies taps and replies as they arrive."""
    operator = settings.operator
    signals = SignalStore(journal)
    log = JournalInbound(journal)
    poller = InboundPoller(
        TelegramUpdates(bot_api, group_id=settings.telegram.group_id),
        log,
        clock,
        channel=TELEGRAM_CHANNEL,
        owners=settings.telegram.owners,
        wait_s=operator.telegram_poll_wait_s,
    )
    sources = JournalSources(CursorStore(journal, signals), signals)
    opened = OpenCommand(
        JournalOpen(journal, signals),
        clients,
        reshower,
        outbox,
        clock,
        destinations=destinations,
        channel=TELEGRAM_CHANNEL,
    )
    commands = Commands(
        outbox, clock, Status(clients, sources), opened, goal, channel=TELEGRAM_CHANNEL
    )
    replies = Replies(
        JournalReplies(journal, signals, OpStore(journal)),
        outbox,
        clock,
        clients,
        channel=TELEGRAM_CHANNEL,
        commands=commands,
        goal=goal,
    )
    health = HealthSource(OPERATOR_SCOPE, sources, clock, operator.link_unreachable_min)
    service = InboundService(
        poller, log, replies, health, clock, interval_s=operator.inbound_interval_s
    )
    return service.run


def goal_command(
    journal: Database,
    clients: Mapping[str, ShikiruClient],
    outbox: Outbox,
    clock: Clock,
    destinations: Destinations,
) -> GoalCommand:
    """Return the /goal command, which journals its submits and replies."""
    submitter = Submitter(JournalSubmits(journal, OpStore(journal)), clients, clock)
    replies = JournalReplies(journal, SignalStore(journal), OpStore(journal))
    return GoalCommand(
        submitter,
        replies,
        clients,
        outbox,
        clock,
        destinations=destinations,
        channel=TELEGRAM_CHANNEL,
    )


def secret_cleanup_job(
    settings: Settings, journal: Database, channel: Channel, clock: Clock
) -> Job:
    """Return the job that deletes the PO's messages which carried a secret (design §7)."""
    cleanup = SecretCleanup(
        JournalInbound(journal), channel, clock, interval_s=settings.operator.inbound_interval_s
    )
    return cleanup.run


def copy_edit_job(settings: Settings, journal: Database, channel: Channel, clock: Clock) -> Job:
    """Return the job that removes the buttons from every copy of a resolved question."""
    signals = SignalStore(journal)
    replies = JournalReplies(journal, signals, OpStore(journal))
    editor = CopyEditor(
        JournalFinalEdits(journal, replies),
        channel,
        clock,
        interval_s=settings.operator.inbound_interval_s,
    )
    return editor.run


def reconcile_job(
    settings: Settings,
    journal: Database,
    clock: Clock,
    *,
    clients: Mapping[str, ShikiruClient],
    reshower: Reshower,
) -> Job:
    """Return the reconciler of issued resolves; it settles what a restart left uncertain."""
    operator = settings.operator
    signals = SignalStore(journal)
    reconciler = ResolveReconciler(
        JournalReconcile(journal, signals, OpStore(journal)),
        JournalSources(CursorStore(journal, signals), signals),
        reshower,
        clients,
        clock,
        lost_command_min=operator.lost_command_min,
        interval_s=operator.liveness_interval_s,
    )
    return reconciler.run


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command with this module's composition; the console entry point calls this."""
    return cli.main(argv, compose, stats_report)


def stats_report(directory: Path) -> str:
    """Return the figures of one instance's journal, which must exist; nothing else is composed."""
    path = directory / JOURNAL_FILE_NAME
    if not path.exists():
        message = f"no journal at {path}"
        raise CompositionError(message)
    try:
        journal = Database.open(path)
    except JournalError as error:
        raise CompositionError(str(error)) from error
    try:
        return Stats(JournalStats(journal)).report()
    finally:
        journal.close()


def schedule_log(journal: Database, reporter: Reporter) -> JournalSchedule:
    """Register notice kinds that need reminders until acknowledged (design §6.4)."""
    return JournalSchedule(
        journal,
        SignalStore(journal),
        MessageStore(journal, reporter),
        OpStore(journal),
        reminder_notice_kinds=frozenset({Kind.PR_REVIEW}),
    )
