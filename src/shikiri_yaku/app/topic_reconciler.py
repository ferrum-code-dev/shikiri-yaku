"""Keep the configured component topics and the shared topic aligned with the journal."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

from shikiri_yaku.ports import (
    TOPIC_CLOSED,
    TOPIC_CLOSING,
    TOPIC_CREATING,
    TOPIC_OPEN,
    UNASSIGNED_THREAD,
    ForumTopics,
    TopicCreationUncertainError,
    TopicLog,
)

GENERAL_TOPIC_NAME = "General"


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
                raise TopicCreationUncertainError(topic.component)
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
