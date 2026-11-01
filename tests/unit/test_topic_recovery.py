"""Recovery of a topic whose creation was uncertain: the PO adopts its thread, or forgets it."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from typing import TYPE_CHECKING, Final

import pytest

from shikiri_yaku.adapters.telegram_topics import (
    TOPIC_CREATING,
    TOPIC_OPEN,
    UNASSIGNED_THREAD,
    TopicReconciler,
    TopicRepair,
)
from shikiri_yaku.bootstrap import main
from shikiri_yaku.instance import JOURNAL_FILE_NAME, instance_lock
from shikiri_yaku.journal import Database
from shikiri_yaku.journal.topics import TopicStore
from shikiri_yaku.ports import ChannelRepairError, ChannelStartupError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

PRODUCT: Final = "shop"
COMPONENT: Final = "api"
GENERAL_THREAD: Final = 5
OPEN_THREAD: Final = 72
ADOPTED_THREAD: Final = 73
CREATED_THREAD: Final = 90

type Row = tuple[str, int]

_CREATING: Final = (TOPIC_CREATING, UNASSIGNED_THREAD)
_OPEN: Final = (TOPIC_OPEN, OPEN_THREAD)
_ADOPTED: Final = (TOPIC_OPEN, ADOPTED_THREAD)
_ADOPT_COMMAND: Final = ["topics", "adopt", PRODUCT, COMPONENT, str(ADOPTED_THREAD)]
_FORGET_COMMAND: Final = ["topics", "forget", PRODUCT, COMPONENT]


class _Forum:
    def __init__(self) -> None:
        self.created: list[str] = []

    async def create(self, name: str) -> int:
        self.created.append(name)
        return CREATED_THREAD

    async def close(self, thread_id: int) -> None:
        raise AssertionError(thread_id)


def _adopt(repair: TopicRepair) -> str:
    return repair.adopt(COMPONENT, ADOPTED_THREAD)


def _forget(repair: TopicRepair) -> str:
    return repair.forget(COMPONENT)


def _save(topics: TopicStore, row: Row | None) -> None:
    if row is not None:
        state, thread_id = row
        topics.save(COMPONENT, thread_id, state)


def _row(topics: TopicStore) -> Row | None:
    topic = topics.get(COMPONENT)
    return None if topic is None else (topic.state, topic.thread_id)


@pytest.mark.parametrize(
    ("before", "change", "accepted", "after"),
    [
        (_CREATING, _adopt, True, _ADOPTED),
        (_CREATING, _forget, True, None),
        (_OPEN, _adopt, False, _OPEN),
        (_OPEN, _forget, False, _OPEN),
        (None, _adopt, False, None),
        (None, _forget, False, None),
    ],
)
def test_adopt_and_forget_change_only_a_creating_topic(
    before: Row | None,
    change: Callable[[TopicRepair], str],
    *,
    accepted: bool,
    after: Row | None,
) -> None:
    database = Database.open(":memory:")
    try:
        topics = TopicStore(database)
        topics.save(None, GENERAL_THREAD, TOPIC_OPEN)
        _save(topics, before)

        if accepted:
            change(TopicRepair(topics))
        else:
            with pytest.raises(ChannelRepairError):
                change(TopicRepair(topics))

        assert _row(topics) == after
    finally:
        database.close()


def _reconciler(topics: TopicStore, forum: _Forum) -> TopicReconciler:
    return TopicReconciler(topics, forum, [COMPONENT], product=PRODUCT)


def test_start_up_does_not_create_a_topic_that_was_adopted() -> None:
    database = Database.open(":memory:")
    try:
        topics = TopicStore(database)
        topics.save(None, GENERAL_THREAD, TOPIC_OPEN)
        _save(topics, _CREATING)
        TopicRepair(topics).adopt(COMPONENT, ADOPTED_THREAD)
        forum = _Forum()

        asyncio.run(_reconciler(topics, forum).run())

        assert forum.created == []
        assert _row(topics) == _ADOPTED
    finally:
        database.close()


def test_start_up_creates_a_topic_that_was_forgotten() -> None:
    database = Database.open(":memory:")
    try:
        topics = TopicStore(database)
        topics.save(None, GENERAL_THREAD, TOPIC_OPEN)
        _save(topics, _CREATING)
        TopicRepair(topics).forget(COMPONENT)
        forum = _Forum()

        asyncio.run(_reconciler(topics, forum).run())

        assert forum.created == [COMPONENT]
        assert _row(topics) == (TOPIC_OPEN, CREATED_THREAD)
    finally:
        database.close()


def test_start_up_refusal_names_both_recovery_commands() -> None:
    database = Database.open(":memory:")
    try:
        topics = TopicStore(database)
        topics.save(None, GENERAL_THREAD, TOPIC_OPEN)
        _save(topics, _CREATING)
        forum = _Forum()

        with pytest.raises(ChannelStartupError) as refusal:
            asyncio.run(_reconciler(topics, forum).run())

        message = str(refusal.value)
        assert f"topics adopt {PRODUCT} {COMPONENT}" in message
        assert f"topics forget {PRODUCT} {COMPONENT}" in message
        assert forum.created == []
    finally:
        database.close()


def _journal(home: Path, row: Row | None) -> Path:
    directory = home / PRODUCT
    directory.mkdir()
    database = Database.open(directory / JOURNAL_FILE_NAME)
    try:
        _save(TopicStore(database), row)
    finally:
        database.close()
    return directory


def _journal_row(directory: Path) -> Row | None:
    database = Database.open(directory / JOURNAL_FILE_NAME)
    try:
        return _row(TopicStore(database))
    finally:
        database.close()


@pytest.mark.parametrize(
    ("command", "running", "exit_code", "after"),
    [
        (_ADOPT_COMMAND, False, 0, _ADOPTED),
        (_ADOPT_COMMAND, True, 1, _CREATING),
        (_FORGET_COMMAND, False, 0, None),
        (_FORGET_COMMAND, True, 1, _CREATING),
    ],
)
def test_topic_commands_change_the_journal_only_when_no_operator_is_running(
    tmp_path: Path,
    command: list[str],
    *,
    running: bool,
    exit_code: int,
    after: Row | None,
) -> None:
    directory = _journal(tmp_path, _CREATING)

    with instance_lock(directory) if running else nullcontext():
        assert main([*command, "--home", str(tmp_path)]) == exit_code

    assert _journal_row(directory) == after
