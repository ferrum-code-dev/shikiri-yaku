"""Telegram's topics: where signals go, and the start-up reconcile of the forum and the journal.

Without a group, a signal goes to the owners' private chats. With a group, every signal of a
component goes to its topic, and the Operator's own signals go to the general topic (design §7,
§13).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol

from shikiri_yaku.adapters.telegram import TELEGRAM_CHANNEL
from shikiri_yaku.domain.schedule import Destination
from shikiri_yaku.domain.signal import OPERATOR_SCOPE, Kind
from shikiri_yaku.ports import ChannelRepairError, ChannelStartupError, CompositionError

if TYPE_CHECKING:
    from collections.abc import Sequence

TOPIC_OPEN: Final = "open"
TOPIC_CLOSED: Final = "closed"
TOPIC_CREATING: Final = "creating"
TOPIC_CLOSING: Final = "closing"
UNASSIGNED_THREAD: Final = 0
GENERAL_TOPIC: Final = "general"
GENERAL_TOPIC_NAME: Final = "General"


class TopicEntry(Protocol):
    """A topic row as the journal stores it; this adapter decides what each state means."""

    @property
    def component(self) -> str | None: ...

    @property
    def thread_id(self) -> int: ...

    @property
    def state(self) -> str: ...


class TopicLog(Protocol):
    """The journal's mapping of each component, and of the general signals, to its topic."""

    def get(self, component: str | None) -> TopicEntry | None: ...

    def save(self, component: str | None, thread_id: int, state: str) -> None: ...

    def replace_if(
        self, component: str | None, expected_state: str, thread_id: int, state: str
    ) -> bool: ...

    def delete_if(self, component: str | None, expected_state: str) -> bool: ...

    def all(self) -> Sequence[TopicEntry]: ...

    def by_thread(self, thread_id: int) -> TopicEntry | None:
        """Return the topic that has `thread_id` in any state, or None."""
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

    def for_signal(self, project: str, kind: Kind) -> list[Destination]:  # noqa: ARG002 -- the port's shape
        if self._group_id is None:
            return [Destination(TELEGRAM_CHANNEL, str(owner)) for owner in self._owners]
        # keep: a component's signals all belong to its topic; the Operator's own belong to General
        component = None if project == OPERATOR_SCOPE else project
        topic = self._log.get(component)
        if topic is None or topic.state != TOPIC_OPEN:
            name = GENERAL_TOPIC if component is None else component
            message = f"Telegram topic '{name}' is not open"
            raise CompositionError(message)
        return [Destination(TELEGRAM_CHANNEL, str(self._group_id), topic.thread_id)]

    def component_of(self, thread: int | None) -> str | None:
        topic = None if thread is None else self._log.by_thread(thread)
        return None if topic is None or topic.state != TOPIC_OPEN else topic.component

    async def prepare(self) -> None:
        if self._reconciler is not None:
            await self._reconciler.run()


class TopicReconciler:
    """Create missing topics and close topics for components removed from configuration."""

    def __init__(
        self, log: TopicLog, forum: ForumTopics, components: Sequence[str], *, product: str
    ) -> None:
        self._log = log
        self._forum = forum
        self._components = tuple(components)
        self._product = product

    async def run(self) -> None:
        active = set(self._components)
        for topic in self._log.all():
            if topic.state == TOPIC_CREATING:
                raise ChannelStartupError(_uncertain_creation(self._product, topic.component))
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


class TopicRepair:
    """The PO's changes to a topic: adopt a thread for it, or forget its uncertain row."""

    def __init__(self, log: TopicLog, components: Sequence[str]) -> None:
        self._log = log
        self._components = tuple(components)

    def adopt(self, name: str, thread_id: int) -> str:
        component = _component(name)
        if component is not None and component not in self._components:
            message = f"no component {name!r} in the config; nothing changed"
            raise ChannelRepairError(message)
        if self._log.get(component) is None:
            # keep: a topic made by hand has no row; an open row stops start-up creating another
            self._log.save(component, thread_id, TOPIC_OPEN)
            return f"Telegram topic {name!r} had no row; it is open with thread {thread_id}"
        if not self._log.replace_if(component, TOPIC_CREATING, thread_id, TOPIC_OPEN):
            raise ChannelRepairError(self._refusal(name, component))
        return f"Telegram topic {name!r} was creating; it is open with thread {thread_id}"

    def forget(self, name: str) -> str:
        component = _component(name)
        if not self._log.delete_if(component, TOPIC_CREATING):
            raise ChannelRepairError(self._refusal(name, component))
        return (
            f"Telegram topic {name!r} was creating; its journal row is removed, so start-up "
            "creates a new topic. A forum topic that the uncertain creation made is not deleted."
        )

    def _refusal(self, name: str, component: str | None) -> str:
        topic = self._log.get(component)
        if topic is None:
            return f"no Telegram topic {name!r} in the journal; nothing changed"
        return f"Telegram topic {name!r} is {topic.state!r}, not creating; nothing changed"


def _component(name: str) -> str | None:
    return None if name == GENERAL_TOPIC else name


def _uncertain_creation(product: str, component: str | None) -> str:
    name = GENERAL_TOPIC if component is None else component
    return (
        f"Telegram topic creation for {name!r} has an uncertain result; inspect the forum. "
        f"If the topic exists, run `shikiri-yaku --product {product} telegram adopt {name} "
        f"<thread-id>`; if it does not, run "
        f"`shikiri-yaku --product {product} telegram forget {name}`."
    )
