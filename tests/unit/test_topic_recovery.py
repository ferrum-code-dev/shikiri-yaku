"""The PO's repair of a topic: adopt a thread for it, or forget an uncertain topic's row."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from typing import TYPE_CHECKING, Final

import pytest

from shikiri_yaku.adapters.telegram_topics import (
    TOPIC_CLOSED,
    TOPIC_CLOSING,
    TOPIC_CREATING,
    TOPIC_OPEN,
    UNASSIGNED_THREAD,
    TopicReconciler,
    TopicRepair,
)
from shikiri_yaku.bootstrap import main
from shikiri_yaku.instance import instance_lock
from shikiri_yaku.journal import JOURNAL_FILE_NAME, Database
from shikiri_yaku.journal.topics import TopicStore
from shikiri_yaku.ports import ChannelRepairError, ChannelStartupError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

PRODUCT: Final = "shop"
COMPONENT: Final = "api"
UNKNOWN_COMPONENT: Final = "web"
GENERAL_THREAD: Final = 5
OPEN_THREAD: Final = 72
ADOPTED_THREAD: Final = 73
CREATED_THREAD: Final = 90

type Row = tuple[str, int]
type Change = Callable[[TopicRepair, str], str]

_CREATING: Final = (TOPIC_CREATING, UNASSIGNED_THREAD)
_OPEN: Final = (TOPIC_OPEN, OPEN_THREAD)
_CLOSING: Final = (TOPIC_CLOSING, OPEN_THREAD)
_CLOSED: Final = (TOPIC_CLOSED, OPEN_THREAD)
_ADOPTED: Final = (TOPIC_OPEN, ADOPTED_THREAD)
_ADOPT_COMMAND: Final = ["telegram", "adopt", COMPONENT, str(ADOPTED_THREAD)]
_ADOPT_UNKNOWN_COMMAND: Final = ["telegram", "adopt", UNKNOWN_COMPONENT, str(ADOPTED_THREAD)]
_FORGET_COMMAND: Final = ["telegram", "forget", COMPONENT]


class _Forum:
    def __init__(self) -> None:
        self.created: list[str] = []

    async def create(self, name: str) -> int:
        self.created.append(name)
        return CREATED_THREAD

    async def close(self, thread_id: int) -> None:
        raise AssertionError(thread_id)


def _adopt(repair: TopicRepair, name: str) -> str:
    return repair.adopt(name, ADOPTED_THREAD)


def _forget(repair: TopicRepair, name: str) -> str:
    return repair.forget(name)


def _save(topics: TopicStore, name: str, row: Row | None) -> None:
    if row is not None:
        state, thread_id = row
        topics.save(name, thread_id, state)


def _row(topics: TopicStore, name: str) -> Row | None:
    topic = topics.get(name)
    return None if topic is None else (topic.state, topic.thread_id)


@pytest.mark.parametrize(
    ("name", "before", "change", "accepted", "after"),
    [
        (COMPONENT, _CREATING, _adopt, True, _ADOPTED),
        (COMPONENT, _CREATING, _forget, True, None),
        (COMPONENT, None, _adopt, True, _ADOPTED),
        (COMPONENT, None, _forget, False, None),
        (COMPONENT, _OPEN, _adopt, False, _OPEN),
        (COMPONENT, _OPEN, _forget, False, _OPEN),
        (COMPONENT, _CLOSING, _adopt, False, _CLOSING),
        (COMPONENT, _CLOSED, _adopt, False, _CLOSED),
        (UNKNOWN_COMPONENT, None, _adopt, False, None),
        (UNKNOWN_COMPONENT, _CREATING, _adopt, False, _CREATING),
    ],
)
def test_repair_changes_only_the_rows_the_table_allows(
    name: str,
    before: Row | None,
    change: Change,
    *,
    accepted: bool,
    after: Row | None,
) -> None:
    database = Database.open(":memory:")
    try:
        topics = TopicStore(database)
        topics.save(None, GENERAL_THREAD, TOPIC_OPEN)
        _save(topics, name, before)
        repair = TopicRepair(topics, [COMPONENT])

        if accepted:
            change(repair, name)
        else:
            with pytest.raises(ChannelRepairError):
                change(repair, name)

        assert _row(topics, name) == after
    finally:
        database.close()


def test_adopt_refuses_general_because_the_built_in_topic_is_not_a_component() -> None:
    database = Database.open(":memory:")
    try:
        topics = TopicStore(database)
        repair = TopicRepair(topics, [COMPONENT])

        with pytest.raises(ChannelRepairError):
            repair.adopt("general", ADOPTED_THREAD)

        assert topics.get(None) is None
        assert topics.get("general") is None
    finally:
        database.close()


def test_start_up_does_not_create_a_topic_that_was_adopted() -> None:
    database = Database.open(":memory:")
    try:
        topics = TopicStore(database)
        topics.save(None, GENERAL_THREAD, TOPIC_OPEN)
        _save(topics, COMPONENT, _CREATING)
        TopicRepair(topics, [COMPONENT]).adopt(COMPONENT, ADOPTED_THREAD)
        forum = _Forum()

        asyncio.run(TopicReconciler(topics, forum, [COMPONENT], product=PRODUCT).run())

        assert forum.created == []
        assert _row(topics, COMPONENT) == _ADOPTED
    finally:
        database.close()


def test_start_up_creates_a_topic_that_was_forgotten() -> None:
    database = Database.open(":memory:")
    try:
        topics = TopicStore(database)
        topics.save(None, GENERAL_THREAD, TOPIC_OPEN)
        _save(topics, COMPONENT, _CREATING)
        TopicRepair(topics, [COMPONENT]).forget(COMPONENT)
        forum = _Forum()

        asyncio.run(TopicReconciler(topics, forum, [COMPONENT], product=PRODUCT).run())

        assert forum.created == [COMPONENT]
        assert _row(topics, COMPONENT) == (TOPIC_OPEN, CREATED_THREAD)
    finally:
        database.close()


def test_start_up_refusal_names_both_recovery_commands() -> None:
    database = Database.open(":memory:")
    try:
        topics = TopicStore(database)
        topics.save(None, GENERAL_THREAD, TOPIC_OPEN)
        _save(topics, COMPONENT, _CREATING)
        forum = _Forum()

        with pytest.raises(ChannelStartupError) as refusal:
            asyncio.run(TopicReconciler(topics, forum, [COMPONENT], product=PRODUCT).run())

        message = str(refusal.value)
        assert f"--product {PRODUCT} telegram adopt {COMPONENT}" in message
        assert f"--product {PRODUCT} telegram forget {COMPONENT}" in message
        assert forum.created == []
    finally:
        database.close()


def _config(home: Path) -> str:
    return (
        f'product = "{PRODUCT}"\n\n[[components]]\nname = "{COMPONENT}"\n'
        f'path = "{home / COMPONENT}"\n\n[channels.telegram]\nowners = [123456789]\n'
    )


def _configured(home: Path) -> Path:
    directory = home / PRODUCT
    directory.mkdir()
    (directory / "config.toml").write_text(_config(home), encoding="utf-8")
    return directory


def _journal(home: Path, row: Row | None) -> Path:
    directory = _configured(home)
    database = Database.open(directory / JOURNAL_FILE_NAME)
    try:
        _save(TopicStore(database), COMPONENT, row)
    finally:
        database.close()
    return directory


def _journal_row(directory: Path) -> Row | None:
    database = Database.open(directory / JOURNAL_FILE_NAME)
    try:
        return _row(TopicStore(database), COMPONENT)
    finally:
        database.close()


@pytest.mark.parametrize(
    ("row", "command", "running", "exit_code", "after"),
    [
        (_CREATING, _ADOPT_COMMAND, False, 0, _ADOPTED),
        (_CREATING, _ADOPT_COMMAND, True, 1, _CREATING),
        (None, _ADOPT_COMMAND, False, 0, _ADOPTED),
        (None, _ADOPT_COMMAND, True, 1, None),
        (None, _ADOPT_UNKNOWN_COMMAND, False, 1, None),
        (_CREATING, _FORGET_COMMAND, False, 0, None),
        (_CREATING, _FORGET_COMMAND, True, 1, _CREATING),
    ],
)
def test_topic_commands_change_the_journal_only_when_allowed(
    tmp_path: Path,
    row: Row | None,
    command: list[str],
    *,
    running: bool,
    exit_code: int,
    after: Row | None,
) -> None:
    directory = _journal(tmp_path, row)

    with instance_lock(directory) if running else nullcontext():
        assert main(["--product", PRODUCT, "--home", str(tmp_path), *command]) == exit_code

    assert _journal_row(directory) == after


def test_adopt_creates_the_journal_of_a_fresh_configured_instance(tmp_path: Path) -> None:
    directory = _configured(tmp_path)

    assert main(["--product", PRODUCT, "--home", str(tmp_path), *_ADOPT_COMMAND]) == 0

    database = Database.open(directory / JOURNAL_FILE_NAME)
    try:
        topics = TopicStore(database)
        assert _row(topics, COMPONENT) == _ADOPTED
        forum = _Forum()
        asyncio.run(TopicReconciler(topics, forum, [COMPONENT], product=PRODUCT).run())
        assert COMPONENT not in forum.created
    finally:
        database.close()


@pytest.mark.parametrize("command", [["stats"], _FORGET_COMMAND])
def test_commands_that_need_an_existing_journal_do_not_create_one(
    tmp_path: Path, command: list[str]
) -> None:
    directory = _configured(tmp_path)

    assert main(["--product", PRODUCT, "--home", str(tmp_path), *command]) == 1

    assert not (directory / JOURNAL_FILE_NAME).exists()
