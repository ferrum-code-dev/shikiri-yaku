from typing import Final, Literal

from pydantic import PositiveInt, TypeAdapter

type AnswerTap = tuple[Literal["a"], PositiveInt, PositiveInt]
type DetailsTap = tuple[Literal["d"], PositiveInt]
type AcknowledgeTap = tuple[Literal["k"], PositiveInt]
type SubmitTap = tuple[Literal["s"], PositiveInt]
type InstallTap = tuple[Literal["i", "x"], PositiveInt, PositiveInt]
type Callback = AnswerTap | DetailsTap | AcknowledgeTap | SubmitTap | InstallTap

SUBMIT_VERB: Final = "s"
INSTALL_VERB: Final = "i"
DECLINE_VERB: Final = "x"
_CALLBACK = TypeAdapter[Callback](Callback)


def submit_data(op_id: int) -> str:
    """Return the callback data of *Submit again*; the op id is its journal reference."""
    return f"{SUBMIT_VERB}:{op_id}"


def install_data(number: int, op_id: int) -> str:
    """Return the numeric callback that asks Replies to approve this stored install op."""
    return f"{INSTALL_VERB}:{number}:{op_id}"


def decline_data(number: int, op_id: int) -> str:
    """Return the numeric callback that asks Replies to decline this stored install op."""
    return f"{DECLINE_VERB}:{number}:{op_id}"


def decode_callback(data: str) -> Callback:
    """Decode each button verb with its own arity; invalid data raises ValidationError."""
    return _CALLBACK.validate_python(data.split(":"))
