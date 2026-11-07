"""Shared test setup: rendered output is plain text, whatever the CI variables say."""

from __future__ import annotations

from typing import Final

import pytest
import typer.rich_utils as typer_rich_utils

# keep: typer reads these once, at import, so clearing them in a test does not stop colour
_COLOUR_VARIABLES: Final = ("GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS")


@pytest.fixture(autouse=True)
def piped_output(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _COLOUR_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(typer_rich_utils, "FORCE_TERMINAL", False)
