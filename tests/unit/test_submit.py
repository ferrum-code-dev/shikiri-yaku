from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

import pytest

from shikiri_yaku.app.submit import Submitter
from shikiri_yaku.journal.database import Database
from shikiri_yaku.journal.ops import OpStore
from shikiri_yaku.journal.submits import JournalSubmits
from shikiri_yaku.ports import (
    Ack,
    AckStatus,
    ShikiruLinkDownError,
    ShikiruProtocolError,
    ShikiruRejectedError,
    SubmitPayload,
    SubmitStatus,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from shikiri_yaku.ports import Clock, ShikiruClient

NOW: Final = datetime(2026, 10, 28, tzinfo=UTC)
PAYLOAD: Final = SubmitPayload(component="api", request="Add the export")
UPDATE: Final = 41
NEXT_UPDATE: Final = 42
LAST_UPDATE: Final = 43
DONE: Final = Ack(id=7, status=AckStatus.DONE, result={"task_id": "T-9"}, message=None)
QUEUED: Final = Ack(id=7, status=AckStatus.QUEUED, result={"task_id": "T-9"}, message=None)
NO_TASK: Final = Ack(id=7, status=AckStatus.DONE, result=None, message=None)


class _CrashError(Exception):
    """The process dies inside the call: no Shikiru error, so nothing records the outcome."""


class _Client:
    def __init__(self) -> None:
        self.answer: Ack | Exception = DONE
        self.requests: list[str] = []

    async def submit_task(self, request: str) -> Ack:
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class _Clock:
    def now(self) -> datetime:
        return NOW


@pytest.fixture
def database() -> Generator[Database]:
    opened = Database.open(":memory:")
    yield opened
    opened.close()


@pytest.fixture
def client() -> _Client:
    return _Client()


@pytest.fixture
def log(database: Database) -> JournalSubmits:
    return JournalSubmits(database, OpStore(database))


@pytest.fixture
def submitter(log: JournalSubmits, client: _Client) -> Submitter:
    clients = {PAYLOAD.component: cast("ShikiruClient", client)}
    return Submitter(log, clients, cast("Clock", _Clock()))


def test_a_duplicate_update_id_submits_once_and_returns_the_stored_outcome(
    submitter: Submitter, client: _Client, log: JournalSubmits
) -> None:
    first = asyncio.run(submitter.submit(UPDATE, PAYLOAD))
    second = asyncio.run(submitter.submit(UPDATE, PAYLOAD))
    assert first.status == SubmitStatus.CONFIRMED
    assert first.task_id == "T-9"
    assert second == first == log.get(first.id)
    assert client.requests == [PAYLOAD.request]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (DONE, (SubmitStatus.CONFIRMED, "T-9")),
        (QUEUED, (SubmitStatus.CONFIRMED, "T-9")),
        (NO_TASK, (SubmitStatus.UNKNOWN, None)),
        (ShikiruRejectedError("no such component"), (SubmitStatus.FAILED, None)),
        (ShikiruLinkDownError("link down"), (SubmitStatus.UNKNOWN, None)),
        (ShikiruProtocolError("unreadable"), (SubmitStatus.UNKNOWN, None)),
    ],
)
def test_each_answer_settles_the_op_and_is_never_sent_again(
    submitter: Submitter,
    client: _Client,
    log: JournalSubmits,
    answer: Ack | Exception,
    expected: tuple[str, str | None],
) -> None:
    client.answer = answer
    op = asyncio.run(submitter.submit(UPDATE, PAYLOAD))
    again = asyncio.run(submitter.submit(UPDATE, PAYLOAD))
    assert (op.status, op.task_id) == expected
    assert again == op == log.get(op.id)
    assert client.requests == [PAYLOAD.request]


def test_a_crash_after_the_call_leaves_the_goal_unknown_and_unsent(
    submitter: Submitter, client: _Client
) -> None:
    client.answer = _CrashError()
    with pytest.raises(_CrashError):
        asyncio.run(submitter.submit(UPDATE, PAYLOAD))
    client.answer = DONE
    restarted = asyncio.run(submitter.submit(UPDATE, PAYLOAD))
    assert restarted.status == SubmitStatus.UNKNOWN
    assert client.requests == [PAYLOAD.request]


def test_an_op_journaled_but_never_claimed_is_sent_on_replay(
    submitter: Submitter, client: _Client, log: JournalSubmits
) -> None:
    log.intend(UPDATE, PAYLOAD, NOW)
    op = asyncio.run(submitter.submit(UPDATE, PAYLOAD))
    assert op.status == SubmitStatus.CONFIRMED
    assert client.requests == [PAYLOAD.request]


def test_unfinished_lists_only_the_ops_without_a_final_outcome(
    submitter: Submitter, client: _Client, log: JournalSubmits
) -> None:
    asyncio.run(submitter.submit(UPDATE, PAYLOAD))
    client.answer = _CrashError()
    with pytest.raises(_CrashError):
        asyncio.run(submitter.submit(NEXT_UPDATE, PAYLOAD))
    client.answer = ShikiruRejectedError("no such component")
    asyncio.run(submitter.submit(LAST_UPDATE, PAYLOAD))
    assert [op.status for op in log.unfinished()] == [SubmitStatus.ISSUED]
