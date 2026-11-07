"""The composition root and the console entry point: the only module that builds concrete objects.

Only what exists is wired. A port whose adapter is not written yet is not in the composition; its
adapter is added here when it is written. `main` gives the command line this module's composition,
so the command line never imports this module and the concrete objects stay here.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Final

from shikiri_yaku import cli
from shikiri_yaku.adapters.clock import SystemClock
from shikiri_yaku.adapters.commands import ProcessCommandRunner
from shikiri_yaku.adapters.host_lock import FileHostLock
from shikiri_yaku.adapters.install_files import InstanceInstallFiles
from shikiri_yaku.adapters.reasoner_cli import ClaudeRung, CodexRung
from shikiri_yaku.adapters.shikiru_mcp import ShikiruMcpClient, probe_link, stdio_connector
from shikiri_yaku.adapters.stderr import StderrReporter
from shikiri_yaku.adapters.telegram import (
    TELEGRAM_CHANNEL,
    TelegramChannel,
    TelegramChannelCheck,
    TelegramForumTopics,
)
from shikiri_yaku.adapters.telegram_http import HttpBotApi
from shikiri_yaku.adapters.telegram_topics import (
    TelegramDestinations,
    TopicReconciler,
    TopicRepair,
)
from shikiri_yaku.adapters.telegram_updates import TelegramUpdates
from shikiri_yaku.app.assessor import Assessor
from shikiri_yaku.app.commands import Commands
from shikiri_yaku.app.copy_editor import CopyEditor
from shikiri_yaku.app.copy_replacer import CopyReplacer
from shikiri_yaku.app.dispatcher import Dispatcher
from shikiri_yaku.app.event_source import EventSource
from shikiri_yaku.app.goal import GoalCommand
from shikiri_yaku.app.health_source import HealthSource
from shikiri_yaku.app.host_facts import HostFacts
from shikiri_yaku.app.inbound_poller import InboundPoller
from shikiri_yaku.app.inbound_service import InboundService
from shikiri_yaku.app.install_executor import InstallExecutor
from shikiri_yaku.app.install_reask import InstallReasker
from shikiri_yaku.app.install_reconciler import InstallReconciler
from shikiri_yaku.app.liveness_source import LivenessSource
from shikiri_yaku.app.loop_guard import LoopGuard
from shikiri_yaku.app.notifier import Notifier
from shikiri_yaku.app.open_command import OpenCommand
from shikiri_yaku.app.outbox_sender import OutboxSender
from shikiri_yaku.app.project_sources import ProjectSources
from shikiri_yaku.app.reasoner_ladder import LadderReasoner
from shikiri_yaku.app.reconciler import ResolveReconciler
from shikiri_yaku.app.replies import Replies
from shikiri_yaku.app.router import Router
from shikiri_yaku.app.scheduler import Scheduler
from shikiri_yaku.app.secret_cleanup import SecretCleanup
from shikiri_yaku.app.start_reconciler import StartReconciler
from shikiri_yaku.app.stats import Measures, Stats
from shikiri_yaku.app.status import Status
from shikiri_yaku.app.submit import Submitter
from shikiri_yaku.app.tool_check import ToolCheck
from shikiri_yaku.domain.rule_classifier import RuleClassifier
from shikiri_yaku.domain.signal import OPERATOR_SCOPE
from shikiri_yaku.extensions.infrastructure.extension import infrastructure_extension
from shikiri_yaku.extensions.infrastructure.install import (
    InstallAction,
    InstallApprovalPresenter,
)
from shikiri_yaku.extensions.infrastructure.install_replies import InstallApprovalReplies
from shikiri_yaku.extensions.infrastructure.preparation import InstallPreparation
from shikiri_yaku.extensions.notice.extension import notice_extension
from shikiri_yaku.extensions.notice.handler import HEALTH_KEY
from shikiri_yaku.extensions.product.extension import product_extension
from shikiri_yaku.extensions.product.forwarder import ProductForwarder
from shikiri_yaku.extensions.review.extension import review_extension
from shikiri_yaku.instance import HOST_LOCK_FILE_NAME, JOURNAL_FILE_NAME
from shikiri_yaku.journal import (
    CursorStore,
    Database,
    JournalActions,
    JournalError,
    JournalEvents,
    JournalFinalEdits,
    JournalInbound,
    JournalOutbox,
    JournalReplacements,
    JournalRetries,
    JournalRouting,
    JournalSchedule,
    JournalSources,
    JournalStarts,
    JournalWaits,
    MessageStore,
    OpStore,
    Repairs,
    SignalStore,
)
from shikiri_yaku.journal.install_reconcile import JournalInstallReconcile
from shikiri_yaku.journal.install_runs import JournalInstallRuns
from shikiri_yaku.journal.open import JournalOpen
from shikiri_yaku.journal.reconcile import JournalReconcile
from shikiri_yaku.journal.replies import JournalReplies
from shikiri_yaku.journal.stats import JournalStats
from shikiri_yaku.journal.submits import JournalSubmits
from shikiri_yaku.journal.topics import TopicStore
from shikiri_yaku.ports import CompositionError
from shikiri_yaku.settings import SHIKIRU_EXECUTABLE, CliSettings, SettingsError, load_settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from shikiri_yaku.adapters.shikiru_mcp import Connector
    from shikiri_yaku.adapters.telegram import BotApi
    from shikiri_yaku.cli import Job
    from shikiri_yaku.domain.classification import Category, Classifier
    from shikiri_yaku.domain.extension import Extension, Handler
    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import (
        Channel,
        ChannelCheck,
        Clock,
        CommandRunner,
        Destinations,
        Forwarder,
        Guard,
        InstallLog,
        InstallPreparer,
        InstallProposalLog,
        InstallRunner,
        InstallToolCheck,
        Outbox,
        ReasonerRung,
        Reporter,
        Reshower,
        RetryLog,
        RungStates,
        ShikiruClient,
        SignalNumbers,
        StartLog,
        WaitLog,
    )
    from shikiri_yaku.settings import Component, ReasonerSettings, Settings


REMOVED_NOTE: Final = "closed at startup: the component was removed from the config"
INBOUND_HEALTH_SCOPE: Final = "telegram_inbound_batch"
# keep: a copy without buttons that the signal ends, sent before the upgrade, still gets its edit
# keep: a notice with these prefixes still unsent when its signal has closed is never sent
ENDING_NOTICE_PREFIXES: Final = (f"{HEALTH_KEY}:",)
REASONER_SCRATCH_DIR: Final = "reasoner-scratch"
FALLBACK_SHELL: Final = "/bin/sh"
RUNG_CLASSES: Final[Mapping[str, Callable[..., ReasonerRung]]] = {
    "claude": ClaudeRung,
    "codex": CodexRung,
}

type LinkFactory = Callable[[str, Path], Connector]
type BotFactory = Callable[[Settings], BotApi]


@dataclass(frozen=True, slots=True)
class Composition:
    settings: Settings
    clock: Clock
    journal: Database
    link_probes: Mapping[str, Callable[[], Awaitable[None]]]
    channel_check: ChannelCheck
    rung_probes: Mapping[str, Callable[[], Awaitable[None]]]
    jobs: Sequence[Job]
    recover: Callable[[], None]

    def close(self) -> None:
        self.journal.close()


@dataclass(frozen=True, slots=True)
class Reasoning:
    """The configured Reasoner: its rungs, the ladder over them and the assessment of a signal."""

    rungs: Sequence[ReasonerRung]
    ladder: LadderReasoner
    assessor: Assessor


def http_bot(settings: Settings) -> BotApi:
    """Return the Bot API transport of the instance's token file and call timeout."""
    return HttpBotApi(settings.telegram.token_file, settings.operator.telegram_call_timeout_s)


def compose(
    settings: Settings,
    directory: Path,
    *,
    link: LinkFactory = stdio_connector,
    bot: BotFactory = http_bot,
    clock: Clock | None = None,
    commands: CommandRunner | None = None,
) -> Composition:
    """Build the objects of one instance; the caller closes the result.

    `link` opens a project's MCP link and `bot` makes the Telegram transport; tests give fakes.
    `clock` is the system clock unless a test gives one whose waits are short, and `commands`
    runs the host commands of the Reasoner's CLIs, which are the process runner unless a test
    gives a fake.
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
    clock = SystemClock() if clock is None else clock
    commands = ProcessCommandRunner() if commands is None else commands
    reasoning = build_reasoner(settings, directory, journal, clock, commands)
    reporter = StderrReporter()
    clients = {
        component.name: shikiru_client(settings, component, clock, link)
        for component in settings.components
    }
    starts_reconciler = start_reconciler(settings, journal, clock, clients=clients)
    # keep: the reconciler waits for the links to connect; signals and sources wait for its gate
    jobs = [starts_reconciler.run]
    jobs += [
        job
        for component in settings.components
        for job in project_jobs(
            settings,
            component,
            clients[component.name],
            journal,
            clock,
            settled=starts_reconciler.settled,
        )
    ]
    outbox = JournalOutbox(
        journal, MessageStore(journal, reporter), ending_prefixes=ENDING_NOTICE_PREFIXES
    )
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
            product=settings.product,
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
    notifier = Notifier(outbox, destinations, clock)
    infrastructure = settings.handlers.infrastructure
    install_shell = os.environ.get("SHELL", FALLBACK_SHELL)
    install_timeout_s = timedelta(minutes=infrastructure.command_timeout_min).total_seconds()
    install_check = ToolCheck(commands, install_shell, install_timeout_s)
    install_proposals = SignalStore(journal)
    install_preparation = InstallPreparation(
        proposals=install_proposals,
        facts=HostFacts(commands, install_shell, install_timeout_s),
        check=install_check,
    )
    extensions = registered_extensions(
        clients,
        guard,
        JournalRetries(journal, OpStore(journal)),
        clock,
        waits=JournalWaits(journal, OpStore(journal)),
        starts=JournalStarts(journal, OpStore(journal), SignalStore(journal)),
        notifier=notifier,
        install_preparation=install_preparation,
        proposals=install_proposals,
        installs=OpStore(journal),
        quota_wait_max=timedelta(hours=infrastructure.quota_wait_max_h),
        quota_wait_margin=timedelta(minutes=infrastructure.quota_wait_margin_min),
        restart_confirm=timedelta(seconds=infrastructure.restart_confirm_s),
        restart_poll=timedelta(seconds=infrastructure.restart_poll_s),
        product=settings.product,
        numbers=SignalStore(journal),
        outbox=outbox,
        destinations=destinations,
    )
    # keep: copies of an older journal are marked before any signal is routed or any copy is sent
    Repairs(journal).adopt_legacy_standing(
        [copy for extension in extensions for copy in extension.legacy_copies]
    )
    forwarder = ProductForwarder(
        settings.handlers.product.message_title_max_chars,
        clients,
        outbox,
        clock,
        destinations,
        button_label_max_chars=settings.handlers.product.button_label_max_chars,
    )
    host_lock = FileHostLock(
        directory.parent / HOST_LOCK_FILE_NAME, clock, infrastructure.host_lock_poll_s
    )
    install_executor = InstallExecutor(
        JournalInstallRuns(journal, SignalStore(journal), OpStore(journal)),
        clients,
        forwarder,
        install_check,
        commands,
        host=host_lock,
        files=InstanceInstallFiles(directory),
        clock=clock,
        shell=install_shell,
        timeout_s=install_timeout_s,
        tail_lines=infrastructure.install_output_tail_lines,
        forward_retry_s=settings.operator.forward_retry_s,
    )
    assess = None if reasoning is None else reasoning.assessor.assess
    jobs += routing_jobs(
        settings,
        journal,
        clock,
        classifier=RuleClassifier(extensions, assess),
        handlers={extension.category: extension.handler for extension in extensions},
        fallback=forwarder,
        reporter=reporter,
        settled=starts_reconciler.settled,
        standing_on_failure=failure_policy(extensions),
    )
    channel = TelegramChannel(bot_api, clock, outbox)
    jobs.append(outbox_job(settings, outbox, clock, channel))
    goal = goal_command(
        journal,
        clients,
        outbox,
        clock,
        destinations,
        shared_group=settings.telegram.shared_group,
    )
    jobs.append(
        inbound_job(
            settings,
            journal,
            outbox,
            clock,
            clients=clients,
            reshower=forwarder,
            forwarder=forwarder,
            bot_api=bot_api,
            goal=goal,
            destinations=destinations,
            ladder=None if reasoning is None else reasoning.ladder,
            install_runner=install_executor,
        )
    )
    jobs.append(secret_cleanup_job(settings, journal, channel, clock))
    jobs += copy_jobs(settings, journal, reporter, channel, clock)
    jobs.append(reconcile_job(settings, journal, clock, clients=clients, reshower=forwarder))
    jobs.append(
        after_settled(
            starts_reconciler.settled,
            install_reconcile_job(
                settings,
                journal,
                clock,
                clients=clients,
                reshower=forwarder,
                runner=install_executor,
                check=install_check,
                outbox=outbox,
                destinations=destinations,
            ),
        )
    )

    def recover() -> None:
        # keep: operator health signals belong to no component; a restart must not close them
        SignalStore(journal).close_removed((*clients, OPERATOR_SCOPE), REMOVED_NOTE, clock.now())
        goal.recover()
        asyncio.run(destinations.prepare())

    rung_probes = {} if reasoning is None else {rung.name: rung.probe for rung in reasoning.rungs}
    return Composition(
        settings=settings,
        clock=clock,
        journal=journal,
        link_probes=probes,
        channel_check=TelegramChannelCheck(
            bot_api,
            settings.telegram.token_file,
            settings.telegram.group_id,
            settings.telegram.owners,
            allow_other_members=settings.telegram.allow_other_members,
            shared_group=settings.telegram.shared_group,
        ),
        rung_probes=rung_probes,
        jobs=jobs,
        recover=recover,
    )


def build_reasoner(
    settings: Settings,
    directory: Path,
    journal: Database,
    clock: Clock,
    commands: CommandRunner,
) -> Reasoning | None:
    """Return the configured Reasoner, or None when the config has no `[reasoner]` section."""
    config = settings.reasoner
    if config is None:
        return None
    rungs = reasoner_rungs(config, commands, directory / REASONER_SCRATCH_DIR)
    ladder = LadderReasoner(
        rungs,
        clock,
        cooldown=timedelta(minutes=config.cooldown_min),
        max_parallel=config.max_parallel,
    )
    signals = SignalStore(journal)
    sources = JournalSources(CursorStore(journal, signals), signals)
    health = HealthSource(OPERATOR_SCOPE, sources, clock, settings.operator.link_unreachable_min)
    facts = HostFacts(commands, os.environ.get("SHELL", FALLBACK_SHELL), config.timeout_s)
    assessor = Assessor(ladder, facts, health, deadline_s=config.timeout_s)
    return Reasoning(rungs, ladder, assessor)


def reasoner_rungs(
    config: ReasonerSettings, commands: CommandRunner, scratch_root: Path
) -> list[ReasonerRung]:
    """Return one rung per ladder entry, each built by the class of its adapter.

    The rungs run one after another, so each gets an equal share of `reasoner.timeout_s`. A rung
    that hangs cannot use the time reserved for the next rung.
    """
    share_s = config.timeout_s / len(config.ladder)
    return [
        RUNG_CLASSES[rung.adapter](
            rung.model,
            commands,
            effort=rung.effort,
            timeout_s=share_s,
            input_max_chars=config.input_max_chars,
            scratch_root=scratch_root,
        )
        for rung in config.ladder
    ]


def registered_extensions(
    clients: Mapping[str, ShikiruClient],
    guard: Guard,
    retries: RetryLog,
    clock: Clock,
    *,
    waits: WaitLog,
    starts: StartLog,
    notifier: Notifier,
    install_preparation: InstallPreparer,
    proposals: InstallProposalLog,
    installs: InstallLog,
    quota_wait_max: timedelta,
    quota_wait_margin: timedelta,
    restart_confirm: timedelta,
    restart_poll: timedelta,
    product: str,
    numbers: SignalNumbers,
    outbox: Outbox,
    destinations: Destinations,
) -> list[Extension]:
    """Return the extensions of this build, in classification order; the catch-all comes last."""
    install = InstallAction(
        preparation=install_preparation,
        proposals=proposals,
        numbers=numbers,
        installs=installs,
        outbox=outbox,
        clock=clock,
        destinations=destinations,
    )
    infrastructure = infrastructure_extension(
        clients,
        guard,
        retries,
        clock,
        waits=waits,
        starts=starts,
        notifier=notifier,
        install=install,
        quota_wait_max=quota_wait_max,
        quota_wait_margin=quota_wait_margin,
        restart_confirm=restart_confirm,
        restart_poll=restart_poll,
    )
    review = review_extension(
        clients, numbers, outbox, clock, product=product, destinations=destinations
    )
    notice = notice_extension(clients, numbers, outbox, notifier, clock, destinations=destinations)
    return [infrastructure, review, notice, product_extension()]


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
        doctor_argv=[SHIKIRU_EXECUTABLE, "--project", str(component.path), "--json", "doctor"],
    )


def project_jobs(
    settings: Settings,
    component: Component,
    client: ShikiruMcpClient,
    journal: Database,
    clock: Clock,
    *,
    settled: asyncio.Event,
) -> list[Job]:
    """Return the link supervisor and the sources of one project, which wait for `settled`."""
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
    return [client.supervise, after_settled(settled, project_sources.run)]


def failure_policy(extensions: Sequence[Extension]) -> Callable[[Signal], bool]:
    """Return the Router's policy: a failure stands when an extension's predicate says so."""
    predicates = [extension.stands_on_failure for extension in extensions]

    def stands(signal: Signal) -> bool:
        return any(predicate(signal) for predicate in predicates)

    return stands


def routing_jobs(
    settings: Settings,
    journal: Database,
    clock: Clock,
    *,
    classifier: Classifier,
    handlers: Mapping[Category, Handler],
    fallback: Forwarder,
    reporter: Reporter,
    settled: asyncio.Event,
    standing_on_failure: Callable[[Signal], bool],
) -> list[Job]:
    """Return the Dispatcher and the Scheduler, which share one Router and wait for `settled`."""
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
        standing_on_failure=standing_on_failure,
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
    return [after_settled(settled, dispatcher.run), after_settled(settled, scheduler.run)]


def after_settled(settled: asyncio.Event, job: Job) -> Job:
    """Return the job, which starts only once `settled` is set."""

    async def run() -> None:
        await settled.wait()
        await job()

    return run


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
    forwarder: Forwarder,
    bot_api: BotApi,
    goal: GoalCommand,
    destinations: Destinations,
    ladder: RungStates | None,
    install_runner: InstallRunner,
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
        install_approval=InstallApprovalPresenter(),
        approval_expiry_h=settings.handlers.infrastructure.approval_expiry_h,
    )
    commands = Commands(
        outbox,
        clock,
        Status(clients, sources, ladder),
        opened,
        goal,
        destinations=destinations,
        channel=TELEGRAM_CHANNEL,
        name_max_chars=operator.command_name_max_chars,
        shared_group=settings.telegram.shared_group,
    )
    replies = Replies(
        JournalReplies(journal, signals, OpStore(journal), channel_name=TELEGRAM_CHANNEL),
        outbox,
        clock,
        clients,
        channel=TELEGRAM_CHANNEL,
        commands=commands,
        goal=goal,
        install_replies=InstallApprovalReplies(
            OpStore(journal),
            clients,
            forwarder,
            outbox,
            clock,
            approval_expiry_h=settings.handlers.infrastructure.approval_expiry_h,
            runner=install_runner,
        ),
    )
    health = HealthSource(OPERATOR_SCOPE, sources, clock, operator.link_unreachable_min)
    service = InboundService(
        poller,
        log,
        replies,
        health,
        clock,
        interval_s=operator.inbound_interval_s,
        health_scope=INBOUND_HEALTH_SCOPE,
    )
    return service.run


def goal_command(
    journal: Database,
    clients: Mapping[str, ShikiruClient],
    outbox: Outbox,
    clock: Clock,
    destinations: Destinations,
    *,
    shared_group: bool = False,
) -> GoalCommand:
    """Return the /goal command, which journals its submits and replies."""
    submitter = Submitter(
        JournalSubmits(journal, OpStore(journal), channel_name=TELEGRAM_CHANNEL), clients, clock
    )
    replies = JournalReplies(
        journal, SignalStore(journal), OpStore(journal), channel_name=TELEGRAM_CHANNEL
    )
    return GoalCommand(
        submitter,
        replies,
        clients,
        outbox,
        clock,
        destinations=destinations,
        channel=TELEGRAM_CHANNEL,
        shared_group=shared_group,
    )


def secret_cleanup_job(
    settings: Settings, journal: Database, channel: Channel, clock: Clock
) -> Job:
    """Return the job that deletes the PO's messages which carried a secret."""
    cleanup = SecretCleanup(
        JournalInbound(journal), channel, clock, interval_s=settings.operator.inbound_interval_s
    )
    return cleanup.run


def copy_edit_job(settings: Settings, journal: Database, channel: Channel, clock: Clock) -> Job:
    """Return the job that removes the buttons from every copy of a resolved question."""
    signals = SignalStore(journal)
    replies = JournalReplies(journal, signals, OpStore(journal), channel_name=TELEGRAM_CHANNEL)
    editor = CopyEditor(
        JournalFinalEdits(journal, replies, ending_prefixes=ENDING_NOTICE_PREFIXES),
        channel,
        clock,
        interval_s=settings.operator.inbound_interval_s,
    )
    return editor.run


def copy_jobs(
    settings: Settings, journal: Database, reporter: Reporter, channel: Channel, clock: Clock
) -> list[Job]:
    """Return the jobs that finish the copies of resolved questions and delete the replaced ones."""
    messages = MessageStore(journal, reporter)
    return [
        copy_edit_job(settings, journal, channel, clock),
        replacement_job(settings, journal, messages, channel, clock),
    ]


def replacement_job(
    settings: Settings, journal: Database, messages: MessageStore, channel: Channel, clock: Clock
) -> Job:
    """Return the job that deletes the copies a newer copy of the same slot has replaced."""
    replacer = CopyReplacer(
        JournalReplacements(journal, messages),
        channel,
        clock,
        interval_s=settings.operator.inbound_interval_s,
    )
    return replacer.run


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


def install_reconcile_job(
    settings: Settings,
    journal: Database,
    clock: Clock,
    *,
    clients: Mapping[str, ShikiruClient],
    reshower: Reshower,
    runner: InstallRunner,
    check: InstallToolCheck,
    outbox: Outbox,
    destinations: Destinations,
) -> Job:
    """Return the recurring install recovery job."""
    signals = SignalStore(journal)
    reasker = InstallReasker(
        OpStore(journal), InstallApprovalPresenter(), outbox, destinations, clock
    )
    reconciler = InstallReconciler(
        JournalInstallReconcile(journal, signals, OpStore(journal)),
        JournalSources(CursorStore(journal, signals), signals),
        clients,
        runner,
        reshower=reshower,
        check=check,
        reasker=reasker,
        clock=clock,
        lost_command_min=settings.operator.lost_command_min,
        interval_s=settings.operator.liveness_interval_s,
    )
    return reconciler.run


def start_reconciler(
    settings: Settings,
    journal: Database,
    clock: Clock,
    *,
    clients: Mapping[str, ShikiruClient],
) -> StartReconciler:
    """Return the reconciler of engine starts a restart left unsettled; it never starts again.

    Its `settled` event gates the signals and sources, so they never act on an unsettled start.
    """
    infrastructure = settings.handlers.infrastructure
    signals = SignalStore(journal)
    return StartReconciler(
        JournalStarts(journal, OpStore(journal), signals),
        clients,
        clock,
        confirm_within=timedelta(seconds=infrastructure.restart_confirm_s),
        interval_s=infrastructure.restart_poll_s,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command with this module's composition; the console entry point calls this."""
    return cli.main(
        argv,
        compose,
        stats_measures,
        adopt_topic,
        forget_topic,
        commands=ProcessCommandRunner(),
        cli_settings=CliSettings(),
    )


def stats_measures(directory: Path) -> Measures:
    """Return the figures of one instance's journal, which must exist; nothing else is composed."""
    journal = _existing_journal(directory)
    try:
        return Stats(JournalStats(journal)).measure()
    finally:
        journal.close()


def adopt_topic(directory: Path, component: str, thread_id: int) -> str:
    """Mark the topic of a component open with a thread of the forum.

    The component must be named in config.toml: start-up reads only those components' topics.
    """
    names = _component_names(directory)
    # keep: the config was read, so the directory is an instance, and a journal may start here
    journal = _open_journal(directory)
    return _change_topics(journal, names, lambda repair: repair.adopt(component, thread_id))


def forget_topic(directory: Path, component: str) -> str:
    """Delete the creating topic of a component, so that start-up creates one."""
    # keep: forget needs no config, so a damaged config.toml does not stop it
    journal = _existing_journal(directory)
    return _change_topics(journal, (), lambda repair: repair.forget(component))


def _component_names(directory: Path) -> list[str]:
    try:
        settings = load_settings(directory)
    except SettingsError as error:
        raise CompositionError(str(error)) from error
    return [component.name for component in settings.components]


def _change_topics(
    journal: Database, components: Sequence[str], change: Callable[[TopicRepair], str]
) -> str:
    try:
        return change(TopicRepair(TopicStore(journal), components))
    finally:
        journal.close()


def _existing_journal(directory: Path) -> Database:
    # keep: Database.open creates a missing file, which would make a journal out of a typo
    path = directory / JOURNAL_FILE_NAME
    if not path.exists():
        message = f"no journal at {path}"
        raise CompositionError(message)
    return _open_journal(directory)


def _open_journal(directory: Path) -> Database:
    try:
        return Database.open(directory / JOURNAL_FILE_NAME)
    except JournalError as error:
        raise CompositionError(str(error)) from error


def schedule_log(journal: Database, reporter: Reporter) -> JournalSchedule:
    """Build the reminder schedule, which reminds standing messages until the PO stops them."""
    return JournalSchedule(
        journal,
        SignalStore(journal),
        MessageStore(journal, reporter),
        OpStore(journal),
    )
