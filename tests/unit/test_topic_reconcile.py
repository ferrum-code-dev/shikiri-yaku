"""Startup reconciles topic state before topic-dependent signals can be delivered."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Final

from shikiri_yaku.adapters.telegram import TelegramForumTopics
from shikiri_yaku.app.topic_reconciler import TopicReconciler
from shikiri_yaku.journal import Database, TopicStore
from shikiri_yaku.ports import TOPIC_CLOSED, TOPIC_CLOSING, TOPIC_CREATING, TOPIC_OPEN
from tests.support.telegram_bot import FIXTURES

if TYPE_CHECKING:
    from collections.abc import Mapping

GROUP_ID: Final = -1009


class _FixtureBotApi:
    def __init__(self, topics: TopicStore) -> None:
        self.topics = topics
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.created = 0

    async def call(self, method: str, payload: Mapping[str, object]) -> object:
        self.calls.append((method, dict(payload)))
        if method == "createForumTopic":
            name = payload["name"]
            assert isinstance(name, str)
            component = None if name == "General" else name
            topic = self.topics.get(component)
            assert topic is not None
            assert topic.state == TOPIC_CREATING
            reply = _fixture("forum_topic_created.json")
            self.created += 1
            reply["result"]["message_thread_id"] = 72 + self.created
            reply["result"]["name"] = name
            return reply
        if method == "closeForumTopic":
            thread_id = payload["message_thread_id"]
            topic = next(item for item in self.topics.all() if item.thread_id == thread_id)
            assert topic.state == TOPIC_CLOSING
            return _fixture("forum_topic_closed.json")
        raise AssertionError(method)


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_startup_creates_topics_once_and_closes_a_removed_component_topic() -> None:
    database = Database.open(":memory:")
    try:
        topics = TopicStore(database)
        api = _FixtureBotApi(topics)
        forum = TelegramForumTopics(api, GROUP_ID)

        asyncio.run(TopicReconciler(topics, forum, ["api"]).run())
        api_topic = topics.get("api")
        general_topic = topics.get(None)
        assert api_topic is not None
        assert general_topic is not None
        assert (api_topic.component, api_topic.thread_id, api_topic.state) == (
            "api",
            73,
            TOPIC_OPEN,
        )
        assert (general_topic.component, general_topic.thread_id, general_topic.state) == (
            None,
            74,
            TOPIC_OPEN,
        )

        asyncio.run(TopicReconciler(topics, forum, ["api"]).run())
        assert [method for method, _payload in api.calls].count("createForumTopic") == 2

        asyncio.run(TopicReconciler(topics, forum, []).run())
        assert [method for method, _payload in api.calls].count("closeForumTopic") == 1
        closed_topic = topics.get("api")
        assert closed_topic is not None
        assert closed_topic.state == TOPIC_CLOSED
        assert closed_topic.thread_id == 73
        assert not any(method == "deleteForumTopic" for method, _payload in api.calls)
    finally:
        database.close()
