from typing import Literal

from pydantic import PositiveInt, TypeAdapter

type AnswerTap = tuple[Literal["a"], PositiveInt, PositiveInt]
type DetailsTap = tuple[Literal["d"], PositiveInt]
type AcknowledgeTap = tuple[Literal["k"], PositiveInt]
type Callback = AnswerTap | DetailsTap | AcknowledgeTap

_CALLBACK = TypeAdapter[Callback](Callback)


def decode_callback(data: str) -> Callback:
    """Decode each button verb with its own arity; invalid data raises ValidationError."""
    return _CALLBACK.validate_python(data.split(":"))
