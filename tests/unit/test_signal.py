from __future__ import annotations

import pytest

from shikiri_yaku.domain.signal import Kind, Signal


@pytest.mark.parametrize(
    ("kind", "source_id", "expected"),
    [
        (Kind.ESCALATION, "E12", "api:escalation:E12"),
        (Kind.TASK_DONE, "5531", "api:task_done:5531"),
        (Kind.HEALTH, "link:2026-10-27T08:00Z", "api:health:link:2026-10-27T08:00Z"),
    ],
)
def test_signal_id_is_project_kind_and_source_id(kind: Kind, source_id: str, expected: str) -> None:
    signal = Signal(project="api", kind=kind, source_id=source_id, reason="", text="")
    assert signal.id == expected
