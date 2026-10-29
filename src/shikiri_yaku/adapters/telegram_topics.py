"""Telegram's topics: where signals go, and the start-up reconcile of the forum and the journal.

Without a group, a signal goes to the owners' private chats. With a group, a component's
escalations go to its topic, and every other signal goes to the general topic (design §7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from shikiri_yaku.adapters.telegram import TELEGRAM_CHANNEL
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.domain.signal import Kind
from shikiri_yaku.ports import ChannelStartupError, CompositionError

if TYPE_CHECKING:
    from collections.abc import Sequence

TOPIC_OPEN: Final = "open"
TOPIC_CLOSED: Final = "closed"
TOPIC_CREATING: Final = "creating"
TOPIC_CLOSING: Final = "closing"
UNASSIGNED_THREAD: Final = 0
GENERAL_TOPIC: Final = "general"
GENERAL_TOPIC_NAME: Final = "General"


@dataclass(frozen=True, slots=True)
class TopicRecord:
    component: str | None
    thread_id: int
    state: str


class TopicLog(Protocol):
    """The journal's mapping of each component, and of the general signals, to its topic."""

    def get(self, component: str | None) -> TopicRecord | None: ...

    def save(self, component: str | None, thread_id: int, state: str = TOPIC_OPEN) -> None: ...

    def all(self) -> list[TopicRecord]: ...

    def component_of(self, thread_id: int) -> str | None:
        """Return the component of the open topic `thread_id`.

        None for the general topic, and for a topic that is unknown or not open.
        """
        ...


class ForumTopics(Protocol):
    """What the reconcile needs from Telegram's topic API."""

    async def create(self, name: str) -> int: ...

    async def close(self, thread_id: int) -> None: ...


class TelegramDestinations:
    """The Destinations of the Telegram channel: private chats, or the group's topics."""

    def __init__(
        self,
        log: TopicLog,
        *,
        owners: Sequence[int],
        group_id: int | None,
        reconciler: TopicReconciler | None,
    ) -> None:
        self._log = log
        self._owners = tuple(owners)
        self._group_id = group_id
        self._reconciler = reconciler

    def for_signal(self, project: str, kind: Kind) -> list[Destination]:
        if self._group_id is None:
            return [Destination(TELEGRAM_CHANNEL, str(owner)) for owner in self._owners]
        component = project if kind == Kind.ESCALATION else None
        topic = self._log.get(component)
        if topic is None or topic.state != TOPIC_OPEN:
            name = GENERAL_TOPIC if component is None else component
            message = f"Telegram topic '{name}' is not open"
            raise CompositionError(message)
        return [Destination(TELEGRAM_CHANNEL, str(self._group_id), topic.thread_id)]

    def component_of(self, thread: int | None) -> str | None:
        return None if thread is None else self._log.component_of(thread)

    async def prepare(self) -> None:
        if self._reconciler is not None:
            await self._reconciler.run()


class TopicReconciler:
    """Create missing topics and close topics for components removed from configuration."""

    def __init__(self, log: TopicLog, forum: ForumTopics, components: Sequence[str]) -> None:
        self._log = log
        self._forum = forum
        self._components = tuple(components)

    async def run(self) -> None:
        active = set(self._components)
        for topic in self._log.all():
            if topic.state == TOPIC_CREATING:
                raise ChannelStartupError(_uncertain_creation(topic.component))
            if (
                topic.component is not None
                and topic.component not in active
                and topic.state != TOPIC_CLOSED
            ):
                await self._close(topic.component, topic.thread_id)
        for component in self._components:
            await self._ensure(component, component)
        await self._ensure(None, GENERAL_TOPIC_NAME)

    async def _ensure(self, component: str | None, name: str) -> None:
        topic = self._log.get(component)
        if topic is not None and topic.state == TOPIC_OPEN:
            return
        if topic is not None and topic.state == TOPIC_CLOSING:
            await self._close(component, topic.thread_id)
        self._log.save(component, UNASSIGNED_THREAD, TOPIC_CREATING)
        thread_id = await self._forum.create(name)
        self._log.save(component, thread_id, TOPIC_OPEN)

    async def _close(self, component: str | None, thread_id: int) -> None:
        self._log.save(component, thread_id, TOPIC_CLOSING)
        await self._forum.close(thread_id)
        self._log.save(component, thread_id, TOPIC_CLOSED)


def _uncertain_creation(component: str | None) -> str:
    name = GENERAL_TOPIC if component is None else component
    return (
        f"Telegram topic creation for {name!r} has an uncertain result; "
        "inspect the forum before changing its journal state."
    )
