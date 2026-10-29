from typing import Final, Literal

from pydantic import PositiveInt, TypeAdapter

type AnswerTap = tuple[Literal["a"], PositiveInt, PositiveInt]
type DetailsTap = tuple[Literal["d"], PositiveInt]
type AcknowledgeTap = tuple[Literal["k"], PositiveInt]
type SubmitTap = tuple[Literal["s"], PositiveInt]
type Callback = AnswerTap | DetailsTap | AcknowledgeTap | SubmitTap

SUBMIT_VERB: Final = "s"
_CALLBACK = TypeAdapter[Callback](Callback)


def submit_data(op_id: int) -> str:
    """Return the callback data of *Submit again*; the op id is its journal reference."""
    return f"{SUBMIT_VERB}:{op_id}"


def decode_callback(data: str) -> Callback:
    """Decode each button verb with its own arity; invalid data raises ValidationError."""
    return _CALLBACK.validate_python(data.split(":"))
