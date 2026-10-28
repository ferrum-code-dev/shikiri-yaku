"""The PO message of a forwarded signal, a template over Shikiru's structured view (§6.1, §7).

Pure: it builds the text and the buttons and does no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from shikiri_yaku.ports import MESSAGE_TEXT_LIMIT, Button, Message, ResolutionKind

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import AgentOption, EscalationResolution, EscalationView

RECOMMENDED: Final = "★"
CUT_MARK: Final = "…"
DETAILS_LABEL: Final = "Details"
ACKNOWLEDGE_LABEL: Final = "Acknowledge"
TITLE_DECISION: Final = "Decision needed"
TITLE_INFORMATION: Final = "For information"


class Tap:
    """The verb at the start of a button's data, the rest being numbers.

    `a` selects a choice (design §7). `d` and `k` are Details and Acknowledge, a clarification
    to §7 recorded in tasks/plan.md under A9.
    """

    ANSWER: Final = "a"
    DETAILS: Final = "d"
    ACKNOWLEDGE: Final = "k"


REASON_HINTS: Final[Mapping[str, str]] = {
    "agent_auth": "Log in to the agent again in a terminal, then tap Retry.",
    "uncommitted_work": "Commit or discard the work in the task workspace, then tap Retry.",
    "off_task_branch": "Move the work onto the task branch, then tap Retry.",
}

_RESOLUTION_LABELS: Final[Mapping[str, str]] = {
    ResolutionKind.ANSWER: "Reply with text",
    ResolutionKind.RETRY: "Retry",
    ResolutionKind.ACCEPT: "Accept",
    ResolutionKind.CANCEL: "Cancel task",
}


@dataclass(frozen=True, slots=True)
class _Choice:
    option: int
    button: str
    line: str


def escalation_message(
    number: int,
    signal: Signal,
    view: EscalationView,
    *,
    product: str,
    note: str,
    suggestion: str,
) -> Message:
    choices = _choices(view)
    buttons = tuple(
        Button(choice.button, f"{Tap.ANSWER}:{number}:{choice.option}") for choice in choices
    )
    text = _text(
        _header(number, signal, product, TITLE_DECISION),
        view.question or view.escalation.headline,
        "\n".join(choice.line for choice in choices),
        _notes(note, suggestion),
        _hint(signal, view),
    )
    if len(text) <= MESSAGE_TEXT_LIMIT:
        return Message(text, buttons)
    details = Button(DETAILS_LABEL, f"{Tap.DETAILS}:{number}")
    return Message(_cut(text), (*buttons, details))


def information_message(
    number: int,
    signal: Signal,
    *,
    product: str,
    note: str,
    suggestion: str,
) -> Message:
    text = _text(
        _header(number, signal, product, TITLE_INFORMATION),
        signal.text,
        _notes(note, suggestion),
    )
    acknowledge = Button(ACKNOWLEDGE_LABEL, f"{Tap.ACKNOWLEDGE}:{number}")
    return Message(_cut(text), (acknowledge,))


def _hint(signal: Signal, view: EscalationView) -> str:
    offers_retry = any(resolution.kind == ResolutionKind.RETRY for resolution in view.resolutions)
    return REASON_HINTS.get(signal.reason, "") if offers_retry else ""


def _choices(view: EscalationView) -> list[_Choice]:
    options = [
        _option_choice(option_no, option)
        for option_no, option in enumerate(view.agent_options, start=1)
    ]
    resolutions = [
        _resolution_choice(option_no, resolution, marked=not view.agent_options)
        for option_no, resolution in enumerate(view.resolutions, start=len(options) + 1)
        if resolution.kind in _RESOLUTION_LABELS
    ]
    return [*options, *resolutions]


def _option_choice(option_no: int, option: AgentOption) -> _Choice:
    mark = f"{RECOMMENDED} " if option.recommended else ""
    line = _with_consequences(f"{mark}{option_no}. {option.label}", option.consequences)
    return _Choice(option_no, str(option_no), line)


def _resolution_choice(
    option_no: int, resolution: EscalationResolution, *, marked: bool
) -> _Choice:
    label = _RESOLUTION_LABELS[resolution.kind]
    mark = f"{RECOMMENDED} " if marked and resolution.recommended else ""
    line = _with_consequences(f"{mark}{label}", resolution.consequences)
    return _Choice(option_no, label, line)


def _with_consequences(text: str, consequences: str) -> str:
    return f"{text} ({consequences})" if consequences else text


def _header(number: int, signal: Signal, product: str, title: str) -> str:
    parts = (f"S{number}", product, signal.project, signal.task_id, title)
    return " · ".join(part for part in parts if part)


def _notes(note: str, suggestion: str) -> str:
    model_note = f"Model note: {suggestion}" if suggestion else ""
    return _text(f"What I tried: {note}", model_note)


def _text(*blocks: str) -> str:
    return "\n".join(block for block in blocks if block)


def _cut(text: str) -> str:
    if len(text) <= MESSAGE_TEXT_LIMIT:
        return text
    return text[: MESSAGE_TEXT_LIMIT - len(CUT_MARK)] + CUT_MARK
