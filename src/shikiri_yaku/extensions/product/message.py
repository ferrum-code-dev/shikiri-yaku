"""The PO message of a forwarded signal, a template over Shikiru's structured view (§6.1, §7).

Pure: it builds the heading, the text and the buttons and does no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from shikiri_yaku.domain.callback import stop_reminders_data
from shikiri_yaku.domain.outbound import STOP_REMINDERS_LABEL, fit
from shikiri_yaku.ports import (
    MESSAGE_TEXT_LIMIT,
    AgentOption,
    Button,
    Message,
    ResolutionKind,
    escalation_choices,
    join_blocks,
    text_room,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shikiri_yaku.domain.signal import Signal
    from shikiri_yaku.ports import EscalationResolution, EscalationView

RECOMMENDED: Final = "★"
CUT_MARK: Final = "…"
DETAILS_LABEL: Final = "Details"
TITLE_INFORMATION: Final = "For information"


class Tap:
    """The verb at the start of a button's data, the rest being numbers.

    `a` selects a choice (design §7). `d` is Details, a clarification to §7 recorded in
    tasks/plan.md under A9. A Stop reminders button carries its own verb (domain/callback.py).
    """

    ANSWER: Final = "a"
    DETAILS: Final = "d"


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
    title: str,
    in_topic: bool,
    note: str,
    suggestion: str,
    label_max_chars: int,
) -> Message:
    choices = _choices(view, label_max_chars)
    buttons = tuple(
        Button(choice.button, f"{Tap.ANSWER}:{number}:{choice.option}") for choice in choices
    )
    heading = _header(number, signal, title, in_topic=in_topic)
    text = join_blocks(
        view.question or view.escalation.headline,
        "\n".join(choice.line for choice in choices),
        _notes(note, suggestion),
        _hint(signal, view),
    )
    message = Message(text, buttons, heading)
    if len(message.shown()) <= MESSAGE_TEXT_LIMIT:
        return message
    details = Button(DETAILS_LABEL, f"{Tap.DETAILS}:{number}")
    return Message(fit(text, text_room(heading)), (*buttons, details), heading)


def information_message(
    number: int,
    signal: Signal,
    *,
    title: str,
    in_topic: bool,
    note: str,
    suggestion: str,
    standing: bool,
) -> Message:
    """Return a message about a signal without a question; only a standing one has a button.

    A standing message stays true until it is fixed, so it keeps Stop reminders (design §6.1).
    """
    heading = _header(number, signal, title, in_topic=in_topic, label=TITLE_INFORMATION)
    text = join_blocks(signal.text, _notes(note, suggestion))
    buttons = (Button(STOP_REMINDERS_LABEL, stop_reminders_data(number)),) if standing else ()
    return Message(fit(text, text_room(heading)), buttons, heading)


def _hint(signal: Signal, view: EscalationView) -> str:
    offers_retry = any(resolution.kind == ResolutionKind.RETRY for resolution in view.resolutions)
    return REASON_HINTS.get(signal.reason, "") if offers_retry else ""


def _choices(view: EscalationView, label_max_chars: int) -> list[_Choice]:
    return [
        _option_choice(choice.number, choice.value, label_max_chars)
        if isinstance(choice.value, AgentOption)
        else _resolution_choice(choice.number, choice.value, marked=not view.agent_options)
        for choice in escalation_choices(view)
    ]


def _option_choice(option_no: int, option: AgentOption, label_max_chars: int) -> _Choice:
    mark = f"{RECOMMENDED} " if option.recommended else ""
    numbered = f"{mark}{option_no}. {option.label}"
    line = _with_consequences(numbered, option.consequences)
    return _Choice(option_no, fit(numbered, label_max_chars), line)


def _resolution_choice(
    option_no: int, resolution: EscalationResolution, *, marked: bool
) -> _Choice:
    label = _RESOLUTION_LABELS[resolution.kind]
    mark = f"{RECOMMENDED} " if marked and resolution.recommended else ""
    line = _with_consequences(f"{mark}{label}", resolution.consequences)
    return _Choice(option_no, label, line)


def _with_consequences(text: str, consequences: str) -> str:
    return f"{text} ({consequences})" if consequences else text


def _header(number: int, signal: Signal, title: str, *, in_topic: bool, label: str = "") -> str:
    # keep: a message in its component's topic already names the component, so it is left out
    project = "" if in_topic else signal.project
    parts = (project, signal.task_id or "", title, label, f"S{number}")
    return " · ".join(part for part in parts if part)


def _notes(note: str, suggestion: str) -> str:
    tried = f"What I tried: {note}" if note else ""
    model_note = f"Model note: {suggestion}" if suggestion else ""
    return "\n".join(block for block in (tried, model_note) if block)
