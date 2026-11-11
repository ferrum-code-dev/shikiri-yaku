from typing import Final, Literal

from pydantic import PositiveInt, TypeAdapter

type AnswerTap = tuple[Literal["a"], PositiveInt, PositiveInt]
type DetailsTap = tuple[Literal["d"], PositiveInt]
type StopRemindersTap = tuple[Literal["k"], PositiveInt]
type SubmitTap = tuple[Literal["s"], PositiveInt]
type InstallTap = tuple[Literal["i", "x"], PositiveInt, PositiveInt]
type SubmitAllTap = tuple[Literal["p"], PositiveInt]
type RejectAllTap = tuple[Literal["r"], PositiveInt]
type WebTap = tuple[Literal["w", "n"], PositiveInt]
type Callback = (
    AnswerTap
    | DetailsTap
    | StopRemindersTap
    | SubmitTap
    | InstallTap
    | SubmitAllTap
    | RejectAllTap
    | WebTap
)

WEB_ALLOW_VERB: Final = "w"
WEB_DENY_VERB: Final = "n"
STOP_REMINDERS_VERB: Final = "k"
SUBMIT_VERB: Final = "s"
INSTALL_VERB: Final = "i"
DECLINE_VERB: Final = "x"
SUBMIT_ALL_VERB: Final = "p"
REJECT_ALL_VERB: Final = "r"
_CALLBACK = TypeAdapter[Callback](Callback)


def stop_reminders_data(number: int) -> str:
    """Return the callback data of *Stop reminders*: it silences a standing message's reminders."""
    return f"{STOP_REMINDERS_VERB}:{number}"


def submit_data(op_id: int) -> str:
    """Return the callback data of *Submit again*; the op id is its journal reference."""
    return f"{SUBMIT_VERB}:{op_id}"


def submit_all_data(proposal_id: int) -> str:
    """Return the callback data of *Submit all*; the proposal is the Expert turn's update id."""
    return f"{SUBMIT_ALL_VERB}:{proposal_id}"


def reject_all_data(proposal_id: int) -> str:
    """Return the callback data of *Reject all*; the proposal is the Expert turn's update id."""
    return f"{REJECT_ALL_VERB}:{proposal_id}"


def install_data(number: int, op_id: int) -> str:
    """Return the numeric callback that asks Replies to approve this stored install op."""
    return f"{INSTALL_VERB}:{number}:{op_id}"


def decline_data(number: int, op_id: int) -> str:
    """Return the numeric callback that asks Replies to decline this stored install op."""
    return f"{DECLINE_VERB}:{number}:{op_id}"


def decode_callback(data: str) -> Callback:
    """Decode each button verb with its own arity; invalid data raises ValidationError."""
    return _CALLBACK.validate_python(data.split(":"))
