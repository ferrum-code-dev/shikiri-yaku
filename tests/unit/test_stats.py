from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import pytest

from shikiri_yaku import bootstrap
from shikiri_yaku.adapters.stderr import StderrReporter
from shikiri_yaku.app.stats import Stats
from shikiri_yaku.domain.classification import Verdict
from shikiri_yaku.domain.outbound import Purpose
from shikiri_yaku.instance import JOURNAL_FILE_NAME
from shikiri_yaku.journal import Database, MessageStore, NewOp, OpState, OpStore, SignalStore
from shikiri_yaku.journal.signals import NewSignal, SignalState
from shikiri_yaku.journal.stats import JournalStats
from shikiri_yaku.ports import Message

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from shikiri_yaku.app.stats import Measures

NOW: Final = datetime(2026, 10, 28, tzinfo=UTC)
_PRODUCT: Final = "shop"


def _at(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


def _signal(
    database: Database,
    name: str,
    *,
    created: int,
    final: int,
    state: SignalState,
    kind: str = "escalation",
    reason: str = "decision_required",
    category: str = "product",
    verdict: Verdict | None = None,
    queued: tuple[int, ...] = (),
    purpose: Purpose = Purpose.QUESTION,
) -> None:
    number = (
        SignalStore(database)
        .insert_if_absent(NewSignal(name, "api", kind, reason, "text", "T-1", "E-1"), _at(created))
        .row_id
    )
    SignalStore(database).classify(
        number,
        category=category,
        classified_by="rule",
        verdict=verdict,
        handler=None,
        now=_at(created),
    )
    SignalStore(database).transition(number, SignalState.NEW, state, _at(final))
    messages = MessageStore(database, StderrReporter())
    for minute in queued:
        messages.record_outbound(
            signal_number=number,
            channel="telegram",
            chat="1001",
            purpose=purpose,
            body=Message("question"),
            notice_key=None,
            now=_at(minute),
        )


@pytest.fixture
def seeded() -> Generator[Database]:
    database = Database.open(":memory:")
    _signal(
        database,
        "A",
        created=0,
        final=60,
        state=SignalState.RESOLVED,
        verdict=Verdict.HOST_TOOL,
        queued=(10, 20),
    )
    _signal(database, "B", created=0, final=40, state=SignalState.CLOSED)
    _signal(
        database,
        "C",
        created=0,
        final=20,
        state=SignalState.RESOLVED,
        reason="integration_blocked",
        category="infrastructure",
        verdict=Verdict.OTHER,
    )
    _signal(database, "D", created=0, final=500, state=SignalState.FORWARDED, queued=(30,))
    _signal(
        database,
        "E",
        kind="engine_down",
        reason="engine_down",
        created=0,
        final=15,
        state=SignalState.RESOLVED,
        category="infrastructure",
    )
    _signal(
        database,
        "F",
        created=0,
        final=90,
        state=SignalState.RESOLVED,
        queued=(80,),
        purpose=Purpose.APPROVAL,
    )
    ops = OpStore(database)
    ops.intend(NewOp("install-1", "install", "api"), NOW)
    ops.intend(NewOp("install-2", "install", "api", state=OpState.ISSUED), NOW)
    ops.intend(NewOp("resolve-1", "resolve", "api", state=OpState.CONFIRMED), NOW)
    yield database
    database.close()


def _measure(database: Database) -> Measures:
    return Stats(JournalStats(database)).measure()


def test_waits_start_at_the_first_queued_question_and_copies_add_nothing(seeded: Database) -> None:
    measured = _measure(seeded)
    assert (measured.completed, measured.queued) == (4, 2)
    assert measured.waiting == timedelta(minutes=60)


def test_the_share_is_summed_waiting_over_summed_completed_lifetime(seeded: Database) -> None:
    measured = _measure(seeded)
    assert measured.lifetime == timedelta(minutes=210)
    assert measured.share == timedelta(minutes=60) / timedelta(minutes=210)


def test_handled_without_the_po_counts_resolved_signals_with_no_question_or_approval(
    seeded: Database,
) -> None:
    assert _measure(seeded).handled_without_po == {"infrastructure": 2}


def test_the_escalation_mix_counts_reasons_and_names_an_absent_verdict(seeded: Database) -> None:
    measured = _measure(seeded)
    assert measured.reasons == {"decision_required": 4, "integration_blocked": 1}
    assert measured.verdicts == {"host_tool": 1, "other": 1, "absent": 3}


def test_installs_are_counted_by_current_state_and_other_kinds_are_left_out(
    seeded: Database,
) -> None:
    assert _measure(seeded).installs == {"intended": 1, "issued": 1}


def test_install_outcomes_count_recorded_approvals_declines_and_failed_checks(
    seeded: Database,
) -> None:
    seeded.connection.execute(
        "UPDATE ops SET approved_at = ? WHERE identity = ?", (_at(1).isoformat(), "install-2")
    )
    seeded.connection.execute(
        "UPDATE ops SET state = 'declined' WHERE identity = ?", ("install-1",)
    )
    seeded.connection.execute(
        "UPDATE ops SET check_failed_at = ? WHERE identity = ?",
        (_at(2).isoformat(), "install-2"),
    )

    assert _measure(seeded).install_outcomes == {
        "approved": 1,
        "declined": 1,
        "checks failed": 1,
    }


def test_no_completed_escalation_leaves_the_share_unavailable() -> None:
    with closing(Database.open(":memory:")) as database:
        measured = _measure(database)
    assert (measured.completed, measured.share) == (0, None)
    assert measured.install_outcomes == {"approved": 0, "declined": 0, "checks failed": 0}


def _forbidden(*_arguments: object) -> None:
    pytest.fail("stats composed the service")


def test_stats_without_a_journal_fails_and_creates_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap, "compose", _forbidden)
    assert bootstrap.main(["--product", _PRODUCT, "--home", str(tmp_path), "stats"]) == 1
    assert not (tmp_path / _PRODUCT).exists()


def test_stats_prints_the_four_sections_of_a_journal_without_composing_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bootstrap, "compose", _forbidden)
    directory = tmp_path / _PRODUCT
    directory.mkdir()
    with closing(Database.open(directory / JOURNAL_FILE_NAME)) as database:
        _signal(database, "A", created=0, final=60, state=SignalState.RESOLVED, queued=(10,))
    assert bootstrap.main(["--product", _PRODUCT, "--home", str(tmp_path), "stats"]) == 0
    output = capsys.readouterr().out
    titles = ["1. PO wait time", "2. Handled without the PO", "3. Escalation mix", "4. Installs"]
    positions = [output.index(title) for title in titles]
    assert positions == sorted(positions)
    assert "\x1b[" not in output


_STATS_KEYS: Final = {
    "completed",
    "queued",
    "lifetime_seconds",
    "waiting_seconds",
    "share",
    "handled_without_po",
    "reasons",
    "verdicts",
    "installs",
    "install_outcomes",
}


def test_stats_json_is_one_object_with_the_documented_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bootstrap, "compose", _forbidden)
    directory = tmp_path / _PRODUCT
    directory.mkdir()
    with closing(Database.open(directory / JOURNAL_FILE_NAME)) as database:
        _signal(database, "A", created=0, final=60, state=SignalState.RESOLVED, queued=(10,))
    assert bootstrap.main(["--json", "--product", _PRODUCT, "--home", str(tmp_path), "stats"]) == 0
    output = capsys.readouterr().out
    document = json.loads(output)
    assert set(document) == _STATS_KEYS
    assert (document["completed"], document["queued"]) == (1, 1)
    assert document["lifetime_seconds"] == timedelta(minutes=60).total_seconds()
    assert document["waiting_seconds"] == timedelta(minutes=50).total_seconds()
    assert "\x1b[" not in output


def test_stats_json_without_a_journal_prints_nothing_to_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bootstrap, "compose", _forbidden)
    assert bootstrap.main(["--json", "--product", _PRODUCT, "--home", str(tmp_path), "stats"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("✘ ")
    assert len(captured.err.splitlines()) == 1
